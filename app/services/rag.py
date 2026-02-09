"""
RAG Retrieval Service
─────────────────────
Retrieves the most relevant FCRA manual chunks for a given query.
Uses vector similarity search (primary) with full-text search fallback.
"""

from dataclasses import dataclass

from app.config import settings
from app.models.database import get_supabase
from app.services.embeddings import embed_single


@dataclass
class RetrievedChunk:
    id: str
    section_number: str
    section_title: str
    content: str
    similarity: float


async def get_relevant_fcra_sections(
    query: str,
    top_k: int | None = None,
    threshold: float | None = None,
) -> list[RetrievedChunk]:
    """
    Retrieve the most relevant FCRA chunks for a query string.

    Uses vector similarity search as the primary method. If fewer than
    `top_k` results are returned, falls back to full-text search to
    fill the gap.

    Args:
        query: Natural language description of the account/violation.
        top_k: Number of results to return (default from config).
        threshold: Minimum similarity score (default from config).

    Returns:
        List of RetrievedChunk objects, sorted by relevance.
    """
    top_k = top_k or settings.RAG_TOP_K
    threshold = threshold or settings.RAG_MATCH_THRESHOLD
    supabase = get_supabase()

    # 1. Vector similarity search
    query_embedding = embed_single(query)

    vector_result = supabase.rpc("match_fcra_chunks", {
        "query_embedding": query_embedding,
        "match_threshold": threshold,
        "match_count": top_k,
    }).execute()

    chunks = [
        RetrievedChunk(
            id=row["id"],
            section_number=row["section_number"],
            section_title=row.get("section_title", ""),
            content=row["content"],
            similarity=row["similarity"],
        )
        for row in (vector_result.data or [])
    ]

    # 2. Full-text search fallback if vector search returned too few results
    if len(chunks) < top_k:
        remaining = top_k - len(chunks)
        seen_ids = {c.id for c in chunks}

        fts_result = supabase.rpc("search_fcra_text", {
            "search_query": query,
            "result_limit": remaining + 5,  # fetch extra to account for dedup
        }).execute()

        for row in (fts_result.data or []):
            if row["id"] not in seen_ids and len(chunks) < top_k:
                chunks.append(RetrievedChunk(
                    id=row["id"],
                    section_number=row["section_number"],
                    section_title=row.get("section_title", ""),
                    content=row["content"],
                    similarity=row.get("rank", 0.0),
                ))
                seen_ids.add(row["id"])

    return chunks


def format_context_for_prompt(chunks: list[RetrievedChunk]) -> str:
    """
    Format retrieved chunks into a single string suitable for injection
    into a Claude system prompt.

    Each chunk is separated by a divider with its section number for
    easy reference by the model.
    """
    if not chunks:
        return "(No relevant FCRA sections found)"

    parts = []
    for chunk in chunks:
        header = f"── {chunk.section_number}"
        if chunk.section_title:
            header += f": {chunk.section_title}"
        header += " ──"
        parts.append(f"{header}\n{chunk.content}")

    return "\n\n".join(parts)
