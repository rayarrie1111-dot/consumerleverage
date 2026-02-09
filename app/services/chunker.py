"""
FCRA Manual Chunker
───────────────────
Takes raw FCRA manual text (from PDF or .txt) and splits it into
semantically meaningful chunks organized by statutory section.

Supports two input modes:
  1. PDF file  → pdfplumber extracts text → chunker splits
  2. Text file → chunker splits directly

Each chunk preserves:
  - section_number (e.g. "15 U.S.C. § 1681i")
  - section_title  (e.g. "Procedure in case of disputed accuracy")
  - content        (the chunk text, prefixed with section info for context)
  - chunk_index    (position within the section for ordering)
  - token_count    (approximate, via tiktoken)
"""

import re
import io
from dataclasses import dataclass

import pdfplumber
import tiktoken

from app.config import settings


@dataclass
class FcraChunk:
    section_number: str
    section_title: str
    content: str
    chunk_index: int
    token_count: int


# ─── Token counting ───────────────────────────────────────────────

_encoder = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoder.encode(text))


# ─── PDF text extraction ──────────────────────────────────────────

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract all text from a PDF file."""
    text_parts = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n\n".join(text_parts)


def extract_text_from_pdf_bytes(pdf_bytes: bytes) -> str:
    """Extract all text from PDF bytes (for in-memory processing)."""
    text_parts = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)
    return "\n\n".join(text_parts)


# ─── Section splitting ────────────────────────────────────────────

# Patterns that identify FCRA section boundaries in the text.
# These handle multiple common formats found in FCRA manual PDFs.
SECTION_PATTERNS = [
    # "§ 1681a" or "§ 1681e(b)" style
    r"§\s*(\d{4}[a-z]?(?:\([a-z0-9]+\))*)",
    # "15 U.S.C. § 1681i" style
    r"15\s+U\.?S\.?C\.?\s+§\s*(\d{4}[a-z]?(?:\([a-z0-9]+\))*)",
    # "Section 602" through "Section 629" (FCRA section numbers)
    r"Section\s+(\d{3}[a-z]?)",
]

COMBINED_PATTERN = re.compile("|".join(f"(?:{p})" for p in SECTION_PATTERNS), re.IGNORECASE)


def _normalize_section_number(raw_match: str) -> str:
    """Normalize a captured section number to '15 U.S.C. § XXXX' format."""
    # Strip leading/trailing whitespace
    raw = raw_match.strip()

    # If it's already in full format, return as-is
    if raw.lower().startswith("15"):
        return raw

    # If it's a short section number like "602", map to FCRA statute numbers
    # FCRA Sections 601-629 map to 15 U.S.C. §§ 1681-1681x
    if re.match(r"^\d{3}", raw):
        return f"Section {raw}"

    # Otherwise it's a § number like "1681i"
    return f"15 U.S.C. § {raw}"


@dataclass
class RawSection:
    section_number: str
    title: str
    text: str


def split_into_sections(text: str) -> list[RawSection]:
    """
    Split FCRA manual text into sections based on statutory section headers.
    Returns a list of RawSection objects with the full text of each section.
    """
    sections: list[RawSection] = []

    # Find all section header positions
    matches = list(COMBINED_PATTERN.finditer(text))

    if not matches:
        # No section headers found — treat entire text as one section
        return [RawSection(
            section_number="FCRA (unsectioned)",
            title="Full Text",
            text=text.strip()
        )]

    for i, match in enumerate(matches):
        # Determine the section number from whichever capture group matched
        groups = [g for g in match.groups() if g is not None]
        raw_number = groups[0] if groups else match.group()
        section_number = _normalize_section_number(raw_number)

        # Extract section text: from this header to the next header (or end)
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        section_text = text[start:end].strip()

        # Try to extract a title from the first line after the section number
        lines = section_text.split("\n", 2)
        title = ""
        if len(lines) >= 2:
            candidate = lines[1].strip()
            # Title lines are usually short and don't start with "("
            if len(candidate) < 120 and not candidate.startswith("("):
                title = candidate

        sections.append(RawSection(
            section_number=section_number,
            title=title,
            text=section_text
        ))

    return sections


# ─── Chunking within sections ─────────────────────────────────────

def chunk_section(
    section: RawSection,
    max_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> list[FcraChunk]:
    """
    Split a single section into token-limited chunks.
    Each chunk is prefixed with the section number and title for RAG context.
    """
    max_tokens = max_tokens or settings.CHUNK_MAX_TOKENS
    overlap_tokens = overlap_tokens or settings.CHUNK_OVERLAP_TOKENS

    prefix = f"[{section.section_number}]"
    if section.title:
        prefix += f" {section.title}"
    prefix += "\n\n"

    prefix_tokens = count_tokens(prefix)
    available_tokens = max_tokens - prefix_tokens

    if available_tokens <= 0:
        available_tokens = max_tokens  # fallback: skip prefix if it's huge

    # Tokenize the section text
    tokens = _encoder.encode(section.text)

    if len(tokens) <= available_tokens:
        # Entire section fits in one chunk
        content = prefix + section.text
        return [FcraChunk(
            section_number=section.section_number,
            section_title=section.title,
            content=content,
            chunk_index=0,
            token_count=count_tokens(content),
        )]

    # Split into overlapping windows
    chunks: list[FcraChunk] = []
    stride = available_tokens - overlap_tokens
    if stride <= 0:
        stride = available_tokens

    idx = 0
    chunk_i = 0
    while idx < len(tokens):
        window = tokens[idx: idx + available_tokens]
        chunk_text = _encoder.decode(window)
        content = prefix + chunk_text

        chunks.append(FcraChunk(
            section_number=section.section_number,
            section_title=section.title,
            content=content,
            chunk_index=chunk_i,
            token_count=count_tokens(content),
        ))

        idx += stride
        chunk_i += 1

    return chunks


# ─── Main entry point ─────────────────────────────────────────────

def chunk_fcra_manual(
    source: str,
    max_tokens: int | None = None,
    overlap_tokens: int | None = None,
) -> list[FcraChunk]:
    """
    Full pipeline: load file → extract text → split sections → chunk.

    Args:
        source: Path to a .pdf or .txt file containing the FCRA manual.
        max_tokens: Max tokens per chunk (default from config).
        overlap_tokens: Token overlap between chunks (default from config).

    Returns:
        List of FcraChunk objects ready for embedding.
    """
    # Load text based on file type
    if source.lower().endswith(".pdf"):
        raw_text = extract_text_from_pdf(source)
    else:
        with open(source, "r", encoding="utf-8") as f:
            raw_text = f.read()

    if not raw_text.strip():
        raise ValueError(f"No text extracted from {source}")

    # Split into statutory sections
    sections = split_into_sections(raw_text)

    # Chunk each section
    all_chunks: list[FcraChunk] = []
    for section in sections:
        chunks = chunk_section(section, max_tokens, overlap_tokens)
        all_chunks.extend(chunks)

    return all_chunks
