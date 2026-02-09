"""
Embedding Service
─────────────────
Abstracts over embedding providers (OpenAI, Voyage) so the rest of
the codebase just calls `embed_texts()` and gets vectors back.

Supports batching to stay within provider rate limits.
"""

from openai import OpenAI

from app.config import settings


def _get_openai_client() -> OpenAI:
    return OpenAI(api_key=settings.OPENAI_API_KEY)


def embed_texts(texts: list[str], batch_size: int = 100) -> list[list[float]]:
    """
    Embed a list of texts using the configured provider.

    Args:
        texts: List of strings to embed.
        batch_size: Max texts per API call (OpenAI supports up to 2048).

    Returns:
        List of embedding vectors (same order as input texts).
    """
    if settings.EMBEDDING_PROVIDER == "openai":
        return _embed_openai(texts, batch_size)
    elif settings.EMBEDDING_PROVIDER == "voyage":
        return _embed_voyage(texts, batch_size)
    else:
        raise ValueError(f"Unknown embedding provider: {settings.EMBEDDING_PROVIDER}")


def embed_single(text: str) -> list[float]:
    """Embed a single text string. Convenience wrapper."""
    return embed_texts([text])[0]


# ─── OpenAI Provider ──────────────────────────────────────────────

def _embed_openai(texts: list[str], batch_size: int) -> list[list[float]]:
    client = _get_openai_client()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        response = client.embeddings.create(
            model=settings.EMBEDDING_MODEL,
            input=batch,
            dimensions=settings.EMBEDDING_DIMENSIONS,
        )
        # Response embeddings are returned in the same order as input
        batch_embeddings = [item.embedding for item in response.data]
        all_embeddings.extend(batch_embeddings)

    return all_embeddings


# ─── Voyage Provider (placeholder — swap in voyageai SDK when ready) ──

def _embed_voyage(texts: list[str], batch_size: int) -> list[list[float]]:
    """
    Voyage AI embeddings. Requires `pip install voyageai` and
    VOYAGE_API_KEY in your environment.

    Uncomment and configure when you're ready to use Voyage.
    """
    try:
        import voyageai  # type: ignore
    except ImportError:
        raise ImportError(
            "Voyage provider requires `pip install voyageai`. "
            "Or switch to EMBEDDING_PROVIDER=openai in your .env"
        )

    client = voyageai.Client()
    all_embeddings: list[list[float]] = []

    for i in range(0, len(texts), batch_size):
        batch = texts[i: i + batch_size]
        result = client.embed(batch, model=settings.EMBEDDING_MODEL)
        all_embeddings.extend(result.embeddings)

    return all_embeddings
