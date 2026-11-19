"""Text embeddings for pgvector search.

If AI_EMBEDDINGS_MODEL is set, the gateway's /v1/embeddings is used (dimensions are fitted to EMBEDDING_DIM).
Otherwise a deterministic multilingual feature-hashing embedding is used (words, bigrams, char-trigrams) —
no model download, works for every Indian script, consistent across restarts.
"""
from __future__ import annotations

import hashlib
import math
import re

from app.config import settings

DIM = settings.EMBEDDING_DIM
_WORD = re.compile(r"\w+", re.UNICODE)


def _hash(tok: str) -> tuple[int, float]:
    h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
    n = int.from_bytes(h, "little")
    return n % DIM, (1.0 if (n >> 63) & 1 else -1.0)


def local_embed(text: str) -> list[float]:
    vec = [0.0] * DIM
    words = [w.casefold() for w in _WORD.findall(text or "")]
    feats: list[tuple[str, float]] = [(w, 1.0) for w in words]
    feats += [(f"{a}_{b}", 0.7) for a, b in zip(words, words[1:])]
    for w in words:
        padded = f"#{w}#"
        feats += [(padded[i : i + 3], 0.35) for i in range(max(0, len(padded) - 2))]
    for tok, weight in feats:
        idx, sign = _hash(tok)
        vec[idx] += sign * weight
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _fit(v: list[float]) -> list[float]:
    v = list(v[:DIM]) + [0.0] * max(0, DIM - len(v))
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


async def embed(texts: list[str]) -> list[list[float]]:
    if settings.AI_EMBEDDINGS_MODEL and settings.AI_GATEWAY_BASE_URL:
        from app.services.ai.providers import router

        p = next((x for x in router.providers if x.name == "gateway"), None)
        if p is not None:
            resp = await p.get_client().embeddings.create(model=settings.AI_EMBEDDINGS_MODEL, input=texts)
            return [_fit(d.embedding) for d in resp.data]
    return [local_embed(t) for t in texts]


async def embed_one(text: str) -> list[float]:
    return (await embed([text]))[0]


async def embed_one_safe(text: str) -> list[float] | None:
    """For request paths: a failing remote embedding service must not turn a save or a search into an HTTP 500.
    (No silent switch to local hashing — vectors from two models would not be comparable.)"""
    import logging

    try:
        return await embed_one(text)
    except Exception as exc:
        logging.getLogger("kisansetu.embeddings").warning("embedding unavailable (%s) — continuing without a vector",
                                                            type(exc).__name__)
        return None
