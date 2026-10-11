"""Local embeddings (fastembed, ONNX on CPU). No API quota is used."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from functools import lru_cache
from typing import Protocol

import numpy as np

from inspraak.config import get_settings

# paraphrase-multilingual-MiniLM-L12-v2 reads 128 tokens and silently drops the rest
# (checked: two texts that differ only after token 128 get identical vectors). Longer
# texts are therefore cut into windows, each window is embedded, and the mean is stored.
WINDOW_CHARS = 480  # about 100 to 120 tokens of English or French prose
MAX_WINDOWS = 6  # 2,880 characters of a text count; the rest is ignored


def windows(text: str, size: int = WINDOW_CHARS, limit: int = MAX_WINDOWS) -> list[str]:
    """Cut a text into at most `limit` pieces of at most `size` characters, on spaces."""
    text = " ".join(text.split())
    if len(text) <= size:
        return [text]
    out: list[str] = []
    while text and len(out) < limit:
        if len(text) <= size:
            out.append(text)
            break
        cut = text.rfind(" ", 0, size + 1)
        cut = cut if cut > size // 2 else size
        out.append(text[:cut])
        text = text[cut:].lstrip()
    return out


class Embedder(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> np.ndarray: ...


class FastEmbedder:
    def __init__(self, model_name: str | None = None, dim: int | None = None) -> None:
        from fastembed import TextEmbedding

        settings = get_settings()
        self.model_name = model_name or settings.embed_model
        self.dim = dim or settings.embed_dim
        cache_dir = settings.data_dir / "models"
        cache_dir.mkdir(parents=True, exist_ok=True)
        self._model = TextEmbedding(self.model_name, cache_dir=str(cache_dir))

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        pieces: list[str] = []
        owner: list[int] = []
        for i, text in enumerate(texts):
            for piece in windows(text):
                pieces.append(piece)
                owner.append(i)
        vectors = normalise(
            np.array(list(self._model.embed(pieces, batch_size=64)), dtype=np.float32)
        )
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        np.add.at(out, np.array(owner), vectors)
        return normalise(out)


class HashEmbedder:
    """Deterministic stand-in for tests: no model download, stable vectors,
    texts that share words get closer vectors."""

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def embed(self, texts: Sequence[str]) -> np.ndarray:
        import hashlib

        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for token in text.lower().split():
                digest = hashlib.sha1(token.encode("utf-8")).digest()
                out[i, int.from_bytes(digest[:4], "big") % self.dim] += 1.0
        return normalise(out)


def normalise(vectors: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vectors / norms


@lru_cache(maxsize=1)
def get_embedder() -> Embedder:
    return FastEmbedder()


def batched(items: Iterable, size: int) -> Iterable[list]:
    batch: list = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
