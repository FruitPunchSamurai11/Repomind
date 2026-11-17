"""Dense retrieval with BAAI/bge-small-en-v1.5 (CPU), cosine similarity.

Embeddings are L2-normalised, so cosine = dot product. Queries get the
model's recommended retrieval instruction prefix; documents do not. Every
embedding is cached on disk keyed by sha1(model, prefix + text), so a re-run
embeds only new text. The cache is derived data (regenerable) and lives in
data/processed/_embeddings/<owner>__<name>.npz.

Heavy dependencies (sentence-transformers, torch, numpy) are imported lazily
so the rest of RepoMind does not need them.
"""
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "processed" / "_embeddings"
MODEL_NAME = "BAAI/bge-small-en-v1.5"
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_model = None


def get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME, device="cpu")
    return _model


def model_info() -> dict:
    import sentence_transformers
    import torch
    return {"model": MODEL_NAME, "sentence_transformers": sentence_transformers.__version__,
            "torch": torch.__version__}


def _key(text: str) -> str:
    return hashlib.sha1(f"{MODEL_NAME}\x00{text}".encode("utf-8")).hexdigest()


class EmbeddingCache:
    def __init__(self, path: Path, encoder=None, batch_size: int = 32):
        import numpy as np
        self.np = np
        self.path = Path(path)
        self.encoder = encoder  # callable(list[str]) -> array; defaults to bge-small
        self.batch_size = batch_size
        self.vectors: dict[str, "np.ndarray"] = {}
        self.dirty = False
        if self.path.exists():
            data = np.load(self.path, allow_pickle=False)
            self.vectors = dict(zip(data["keys"].tolist(), data["vecs"]))

    def _encode(self, texts: list[str]):
        if self.encoder is not None:
            return self.np.asarray(self.encoder(texts), dtype="float32")
        return get_model().encode(texts, batch_size=self.batch_size, normalize_embeddings=True,
                                  convert_to_numpy=True, show_progress_bar=False).astype("float32")

    def embed(self, texts: list[str]):
        keys = [_key(t) for t in texts]
        missing = list(dict.fromkeys(k for k in keys if k not in self.vectors))
        if missing:
            by_key = dict(zip(keys, texts))
            vecs = self._encode([by_key[k] for k in missing])
            self.vectors.update(zip(missing, vecs))
            self.dirty = True
        return self.np.stack([self.vectors[k] for k in keys]) if keys else self.np.zeros((0, 0), "float32")

    def save(self):
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        keys = list(self.vectors)
        tmp = self.path.with_suffix(".tmp.npz")
        self.np.savez(tmp, keys=self.np.array(keys), vecs=self.np.stack([self.vectors[k] for k in keys]))
        tmp.replace(self.path)
        self.dirty = False


class DenseIndex:
    """Cosine-similarity index over (key, text) documents."""

    def __init__(self, keys: list[str], texts: list[str], cache: EmbeddingCache):
        self.keys = keys
        self.cache = cache
        self.matrix = cache.embed(texts)
        cache.save()

    def search_many(self, queries: list[str], limit: int = 100) -> list[list[tuple[str, float]]]:
        np = self.cache.np
        q = self.cache.embed([QUERY_PREFIX + t for t in queries])
        self.cache.save()
        sims = q @ self.matrix.T
        out = []
        for row in sims:
            k = min(limit, len(self.keys))
            top = np.argpartition(-row, k - 1)[:k] if k < len(self.keys) else np.arange(len(self.keys))
            top = sorted(top, key=lambda i: (-row[i], self.keys[i]))
            out.append([(self.keys[i], float(row[i])) for i in top])
        return out
