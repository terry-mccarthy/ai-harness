"""BM25 + dense hybrid codebase search over a local repo.

Pure stdlib + math for BM25. Dense search delegates to an injected ``Embedder``
that exposes ``embed_one(text)`` and ``embed_many(texts)``. Hybrid mode merges
both rankings via Reciprocal Rank Fusion (RRF, k=60).
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

_SKIP_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "target", "dist", "build", ".next", "coverage", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".tox",
})
_MAX_FILE_BYTES = 1_000_000
_CHUNK_LINES = 200
_CHUNK_OVERLAP = 20
_WORD_SPLIT = re.compile(r"[^a-zA-Z0-9]+")
_CAMEL_SPLIT = re.compile(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_BM25_K1 = 1.5
_BM25_B = 0.75
_RRF_K = 60
_VALID_MODES = ("bm25", "semantic", "hybrid")


class Embedder(Protocol):
    def embed_one(self, text: str) -> list[float]: ...
    def embed_many(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class Chunk:
    file: str            # repo-relative POSIX path
    start_line: int      # 1-indexed inclusive
    end_line: int        # 1-indexed inclusive
    text: str


@dataclass
class Index:
    root: Path
    chunks: list[Chunk]
    _doc_tokens: list[list[str]] = field(default_factory=list)
    _doc_freq: dict[str, int] = field(default_factory=dict)
    _doc_len: list[int] = field(default_factory=list)
    _avgdl: float = 0.0
    embeddings: list[list[float]] | None = None


@dataclass(frozen=True)
class SearchResult:
    file: str
    start_line: int
    end_line: int
    text: str
    score: float


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on non-alphanumeric AND camelCase boundaries."""
    tokens: list[str] = []
    for word in _WORD_SPLIT.split(text):
        if not word:
            continue
        for sub in _CAMEL_SPLIT.split(word):
            sub = sub.lower()
            if len(sub) >= 2:
                tokens.append(sub)
    return tokens


def _walk_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(p in _SKIP_DIRS for p in parts):
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def _chunk_file(rel_path: str, text: str) -> list[Chunk]:
    lines = text.splitlines()
    if not lines:
        return []
    if len(lines) <= _CHUNK_LINES:
        return [Chunk(rel_path, 1, len(lines), text)]
    chunks: list[Chunk] = []
    step = _CHUNK_LINES - _CHUNK_OVERLAP
    for start in range(0, len(lines), step):
        end = min(start + _CHUNK_LINES, len(lines))
        chunk_text = "\n".join(lines[start:end])
        chunks.append(Chunk(rel_path, start + 1, end, chunk_text))
        if end == len(lines):
            break
    return chunks


def build_index(root: Path | str) -> Index:
    """Walk the repo, chunk every text file, build a BM25 index."""
    root = Path(root).resolve()
    chunks: list[Chunk] = []
    for path in _walk_files(root):
        text = _read_text(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix()
        chunks.extend(_chunk_file(rel, text))

    doc_tokens = [_tokenize(c.text) for c in chunks]
    doc_len = [len(t) for t in doc_tokens]
    df: Counter[str] = Counter()
    for tokens in doc_tokens:
        for term in set(tokens):
            df[term] += 1
    avgdl = (sum(doc_len) / len(doc_len)) if doc_len else 0.0

    return Index(
        root=root,
        chunks=chunks,
        _doc_tokens=doc_tokens,
        _doc_freq=dict(df),
        _doc_len=doc_len,
        _avgdl=avgdl,
    )


def _bm25_score(query_tokens: list[str], doc_tokens: list[str], doc_len: int, idx: Index) -> float:
    if not doc_len or not query_tokens:
        return 0.0
    tf = Counter(doc_tokens)
    n_docs = len(idx.chunks)
    score = 0.0
    for term in query_tokens:
        if term not in tf:
            continue
        df = idx._doc_freq.get(term, 0)
        idf = math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)
        freq = tf[term]
        norm = 1 - _BM25_B + _BM25_B * (doc_len / idx._avgdl if idx._avgdl else 1.0)
        score += idf * (freq * (_BM25_K1 + 1)) / (freq + _BM25_K1 * norm)
    return score


def embed_index(index: Index, embedder: Embedder) -> None:
    """Populate ``index.embeddings`` by embedding every chunk's text."""
    if not index.chunks:
        index.embeddings = []
        return
    index.embeddings = embedder.embed_many([c.text for c in index.chunks])


def _to_result(index: Index, doc_idx: int, score: float) -> SearchResult:
    chunk = index.chunks[doc_idx]
    return SearchResult(
        file=chunk.file,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        text=chunk.text,
        score=round(score, 4),
    )


def _bm25_rank(index: Index, query_tokens: list[str]) -> list[tuple[int, float]]:
    if not query_tokens or not index.chunks:
        return []
    scored = [
        (i, _bm25_score(query_tokens, tokens, dl, index))
        for i, (tokens, dl) in enumerate(zip(index._doc_tokens, index._doc_len))
    ]
    scored = [pair for pair in scored if pair[1] > 0]
    scored.sort(key=lambda pair: -pair[1])
    return scored


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0 if either vector is zero-length."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _semantic_rank(index: Index, query: str, embedder: Embedder) -> list[tuple[int, float]]:
    if not index.chunks or index.embeddings is None:
        return []
    q_vec = embedder.embed_one(query)
    scored = [(i, _cosine(q_vec, v)) for i, v in enumerate(index.embeddings)]
    scored = [pair for pair in scored if pair[1] > 0]
    scored.sort(key=lambda pair: -pair[1])
    return scored


def _rrf_merge(*rankings: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Reciprocal Rank Fusion. ``score(d) = sum 1/(k + rank_i(d))`` over each ranked list."""
    fused: dict[int, float] = {}
    for ranked in rankings:
        for rank, (doc_idx, _score) in enumerate(ranked):
            fused[doc_idx] = fused.get(doc_idx, 0.0) + 1.0 / (_RRF_K + rank + 1)
    return sorted(fused.items(), key=lambda pair: -pair[1])


def search(
    index: Index,
    query: str,
    top_k: int = 5,
    mode: str = "bm25",
    embedder: Embedder | None = None,
) -> list[SearchResult]:
    """Return top-k chunks ranked by ``mode``: ``bm25``, ``semantic``, or ``hybrid``."""
    if mode not in _VALID_MODES:
        raise ValueError(f"mode {mode!r} not supported; expected one of {_VALID_MODES}")
    if mode in ("semantic", "hybrid") and embedder is None:
        raise ValueError(f"mode {mode!r} requires an embedder")
    if not index.chunks:
        return []

    if mode == "bm25":
        ranked = _bm25_rank(index, _tokenize(query))
    elif mode == "semantic":
        ranked = _semantic_rank(index, query, embedder)  # type: ignore[arg-type]
    else:  # hybrid
        bm25 = _bm25_rank(index, _tokenize(query))
        semantic = _semantic_rank(index, query, embedder)  # type: ignore[arg-type]
        ranked = _rrf_merge(bm25, semantic) or bm25

    return [_to_result(index, doc_idx, score) for doc_idx, score in ranked[:top_k]]
