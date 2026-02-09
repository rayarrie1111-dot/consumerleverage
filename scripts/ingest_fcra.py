#!/usr/bin/env python3
"""
FCRA Manual Ingestion Script
─────────────────────────────
One-time (or re-runnable) script that:
  1. Reads your FCRA manual (PDF or text file)
  2. Splits it into token-limited chunks by statutory section
  3. Embeds each chunk via OpenAI/Voyage
  4. Stores chunks + embeddings in Supabase pgvector
  5. Generates the fcra_sections.json lookup table for citation verification

Usage:
  python scripts/ingest_fcra.py path/to/fcra_manual.pdf
  python scripts/ingest_fcra.py path/to/fcra_manual.txt --clear

Flags:
  --clear   Delete all existing chunks before ingesting (fresh start)
  --dry-run Parse and chunk the file, print stats, but don't embed or store
"""

import sys
import os
import json
import argparse
import time

# Add project root to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.services.chunker import chunk_fcra_manual, FcraChunk
from app.services.embeddings import embed_texts
from app.models.database import get_supabase


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ingest an FCRA manual into the vector store"
    )
    parser.add_argument(
        "file",
        help="Path to the FCRA manual file (.pdf or .txt)",
    )
    parser.add_argument(
        "--clear",
        action="store_true",
        help="Delete all existing FCRA chunks before ingesting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and chunk only — don't embed or store",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help=f"Max tokens per chunk (default: {settings.CHUNK_MAX_TOKENS})",
    )
    parser.add_argument(
        "--overlap",
        type=int,
        default=None,
        help=f"Token overlap between chunks (default: {settings.CHUNK_OVERLAP_TOKENS})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=50,
        help="Embedding batch size (default: 50)",
    )
    return parser.parse_args()


def print_chunk_stats(chunks: list[FcraChunk]) -> None:
    """Print summary statistics about the chunks."""
    total_tokens = sum(c.token_count for c in chunks)
    sections = set(c.section_number for c in chunks)

    print(f"\n{'='*60}")
    print(f"  FCRA Manual Chunking Summary")
    print(f"{'='*60}")
    print(f"  Total chunks:    {len(chunks)}")
    print(f"  Total tokens:    {total_tokens:,}")
    print(f"  Avg tokens/chunk: {total_tokens // len(chunks) if chunks else 0}")
    print(f"  Unique sections: {len(sections)}")
    print(f"{'='*60}")

    print(f"\n  Sections found:")
    for section in sorted(sections):
        section_chunks = [c for c in chunks if c.section_number == section]
        section_tokens = sum(c.token_count for c in section_chunks)
        print(f"    {section} — {len(section_chunks)} chunk(s), {section_tokens} tokens")

    print()


def generate_sections_json(chunks: list[FcraChunk]) -> None:
    """
    Generate the fcra_sections.json lookup table from the ingested chunks.
    This is used by the citation verifier to validate AI-generated citations.
    """
    sections = sorted(set(c.section_number for c in chunks))

    # Also generate the full 15 U.S.C. § format for sections that aren't already
    full_sections = set()
    for s in sections:
        full_sections.add(s)
        # If section has subsections in the content, add those too
        for chunk in chunks:
            if chunk.section_number == s:
                import re
                found = re.findall(
                    r"15\s+U\.?S\.?C\.?\s+§\s*(\d{4}[a-z]?(?:-\d+)?(?:\([a-z0-9]+\))*)",
                    chunk.content,
                    re.IGNORECASE,
                )
                for match in found:
                    full_sections.add(f"15 U.S.C. § {match}")

    output_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "data", "fcra_sections.json"
    )

    data = {
        "description": "Valid FCRA section numbers extracted from the ingested manual. Used by citation_verifier.py.",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_sections": sections,
        "sections": sorted(full_sections),
    }

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  Generated {output_path}")
    print(f"  {len(full_sections)} valid section references indexed")


def clear_existing_chunks() -> None:
    """Delete all rows from the fcra_chunks table."""
    supabase = get_supabase()
    # Delete all rows (Supabase requires a filter, use a truthy condition)
    supabase.table("fcra_chunks").delete().gte("created_at", "1970-01-01").execute()
    print("  Cleared all existing FCRA chunks from database")


def store_chunks(chunks: list[FcraChunk], batch_size: int) -> None:
    """Embed chunks and store in Supabase."""
    supabase = get_supabase()

    print(f"\n  Embedding {len(chunks)} chunks (batch size: {batch_size})...")

    # Embed all chunk texts
    texts = [c.content for c in chunks]
    embeddings = embed_texts(texts, batch_size=batch_size)

    print(f"  Embedding complete. Storing in Supabase...")

    # Insert into database in batches
    insert_batch_size = 50
    for i in range(0, len(chunks), insert_batch_size):
        batch_chunks = chunks[i: i + insert_batch_size]
        batch_embeddings = embeddings[i: i + insert_batch_size]

        rows = []
        for chunk, embedding in zip(batch_chunks, batch_embeddings):
            rows.append({
                "section_number": chunk.section_number,
                "section_title": chunk.section_title,
                "content": chunk.content,
                "chunk_index": chunk.chunk_index,
                "token_count": chunk.token_count,
                "embedding": embedding,
            })

        supabase.table("fcra_chunks").insert(rows).execute()

        stored = min(i + insert_batch_size, len(chunks))
        print(f"    Stored {stored}/{len(chunks)} chunks", end="\r")

    print(f"\n  All {len(chunks)} chunks stored successfully")


def main() -> None:
    args = parse_args()

    # Validate file exists
    if not os.path.isfile(args.file):
        print(f"Error: File not found: {args.file}")
        sys.exit(1)

    print(f"\n  Loading FCRA manual from: {args.file}")

    # Step 1: Chunk the manual
    chunks = chunk_fcra_manual(
        source=args.file,
        max_tokens=args.max_tokens,
        overlap_tokens=args.overlap,
    )

    # Step 2: Print stats
    print_chunk_stats(chunks)

    # Step 3: Generate sections lookup table (always, even on dry-run)
    generate_sections_json(chunks)

    if args.dry_run:
        print("  [DRY RUN] Skipping embedding and storage.")
        print("  Re-run without --dry-run to embed and store in Supabase.")
        return

    # Step 4: Clear existing data if requested
    if args.clear:
        clear_existing_chunks()

    # Step 5: Embed and store
    store_chunks(chunks, args.batch_size)

    print(f"\n  FCRA ingestion complete!")
    print(f"  Your RAG pipeline is ready. The AI engine can now query")
    print(f"  relevant FCRA sections via the match_fcra_chunks() function.\n")


if __name__ == "__main__":
    main()
