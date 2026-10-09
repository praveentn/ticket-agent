"""Embedding providers.

fastembed -> BAAI/bge-small-en-v1.5 (384-d, ONNX, runs locally on CPU; model baked into the image).
hash      -> deterministic feature-hashing fallback; no model download, weaker semantics. Used if fastembed fails.
"""

import hashlib
import logging
import math
import re
from functools import lru_cache

from app.config import settings

log = logging.getLogger(__name__)
_TOKEN = re.compile(r"[a-z0-9]+")


class HashEmbedder:
    name = "hash"

    def __init__(self, dim: int):
        self.dim = dim

    def _embed(self, text: str) -> list[float]:
        tokens = _TOKEN.findall(text.lower())
        feats = tokens + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        vec = [0.0] * self.dim
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "big")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [self._embed(t) for t in texts]


class FastEmbedder:
    name = "fastembed"

    def __init__(self, model_name: str):
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model_name=model_name)

    def embed_query(self, text: str) -> list[float]:
        return next(iter(self.model.query_embed([text]))).tolist()

    def embed_passages(self, texts: list[str]) -> list[list[float]]:
        return [v.tolist() for v in self.model.passage_embed(texts)]


@lru_cache
def get_embedder() -> HashEmbedder | FastEmbedder:
    if settings.embedding_provider == "fastembed":
        try:
            return FastEmbedder(settings.embedding_model)
        except Exception:  # model missing/offline: degrade rather than fail retrieval entirely
            log.exception("fastembed unavailable, falling back to hash embeddings")
    return HashEmbedder(settings.embedding_dim)


def to_pgvector(vec: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"
