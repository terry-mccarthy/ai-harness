"""Unit tests for semantic + hybrid search over the BM25 index.

These tests use a deterministic in-memory embedder so they run without Ollama.
A live Ollama smoke test lives in ``tests/test_live.py`` (separate file, opt-in).
"""
from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from architect_server.search import build_index, embed_index, search


class BagOfWordsEmbedder:
    """Deterministic hash-bag embedder.

    Each lowercased token contributes 1 to dimension ``md5(token) % dim``; the
    resulting vector is L2-normalised. Texts that share tokens get high cosine
    similarity. Stable across Python runs (unlike ``hash()``).
    """

    def __init__(self, dim: int = 64):
        self.dim = dim

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * self.dim
        for token in text.lower().split():
            idx = int(hashlib.md5(token.encode()).hexdigest(), 16) % self.dim
            v[idx] += 1.0
        norm = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / norm for x in v]

    def embed_one(self, text: str) -> list[float]:
        return self._vec(text)

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    # Chunk A: lots of "auth" tokens — BM25 would score high on "auth login user".
    _write(tmp_path, "auth.py", "auth auth auth login user password verify session token cookie\n")
    # Chunk B: about graph databases — BM25 won't match "auth login user" at all,
    # but semantic search on "user identity authentication" still overlaps somewhat.
    _write(tmp_path, "graph.py", "graph database neo4j cypher node edge traversal pagerank\n")
    # Chunk C: about HTTP servers
    _write(tmp_path, "http.py", "http server route handler middleware request response status\n")
    return tmp_path


def test_bm25_mode_does_not_need_embedder(repo: Path):
    """bm25 mode keeps working when no embedder is provided (backwards compat)."""
    index = build_index(repo)
    results = search(index, query="auth login user", top_k=3, mode="bm25")
    assert results
    assert results[0].file == "auth.py"


def test_semantic_mode_requires_embedder(repo: Path):
    index = build_index(repo)
    with pytest.raises(ValueError, match="embedder"):
        search(index, query="auth login", top_k=3, mode="semantic")


def test_embed_index_populates_embeddings(repo: Path):
    index = build_index(repo)
    assert index.embeddings is None
    embed_index(index, BagOfWordsEmbedder())
    assert index.embeddings is not None
    assert len(index.embeddings) == len(index.chunks)
    assert all(len(v) == 64 for v in index.embeddings)


def test_semantic_search_ranks_by_cosine(repo: Path):
    """Semantic search uses the embedder, not BM25 — query tokens that overlap
    with chunk content rank that chunk first even when BM25 would be tied."""
    index = build_index(repo)
    embedder = BagOfWordsEmbedder()
    embed_index(index, embedder)
    results = search(
        index,
        query="graph database neo4j",
        top_k=3,
        mode="semantic",
        embedder=embedder,
    )
    assert results
    assert results[0].file == "graph.py"
    assert results[0].score > 0


def test_hybrid_combines_bm25_and_semantic_via_rrf(repo: Path):
    """Hybrid mode merges BM25 and semantic rankings via Reciprocal Rank Fusion.

    A query that BM25 scores zero for one chunk but semantic ranks first should
    still surface that chunk in hybrid mode.
    """
    index = build_index(repo)
    embedder = BagOfWordsEmbedder()
    embed_index(index, embedder)
    results = search(
        index,
        query="graph database neo4j cypher",
        top_k=3,
        mode="hybrid",
        embedder=embedder,
    )
    files = [r.file for r in results]
    assert "graph.py" in files
    assert results[0].file == "graph.py"


def test_hybrid_falls_back_to_bm25_when_semantic_returns_nothing(repo: Path):
    """If the embedder gives identical zero-similarity vectors for everything,
    hybrid should still return BM25 hits rather than dropping the result."""

    class ZeroEmbedder:
        def embed_one(self, text):
            return [0.0] * 8

        def embed_many(self, texts):
            return [[0.0] * 8 for _ in texts]

    index = build_index(repo)
    embed_index(index, ZeroEmbedder())
    results = search(
        index,
        query="auth login user",
        top_k=3,
        mode="hybrid",
        embedder=ZeroEmbedder(),
    )
    assert results
    assert results[0].file == "auth.py"


def test_invalid_mode_raises(repo: Path):
    index = build_index(repo)
    with pytest.raises(ValueError, match="mode"):
        search(index, query="x", mode="lexical")


def test_embed_index_is_idempotent(repo: Path):
    """A cached Index is reused across modes; re-embedding would be wasteful."""

    class CountingEmbedder(BagOfWordsEmbedder):
        def __init__(self):
            super().__init__()
            self.calls = 0

        def embed_many(self, texts):
            self.calls += 1
            return super().embed_many(texts)

    index = build_index(repo)
    embedder = CountingEmbedder()
    embed_index(index, embedder)
    embed_index(index, embedder)
    assert embedder.calls == 1
