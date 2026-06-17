"""Unit tests for architect_server.search — pure Python, no network."""
from pathlib import Path

import pytest

from architect_server.search import build_index, search


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@pytest.fixture
def fixture_repo(tmp_path: Path) -> Path:
    _write(tmp_path, "auth/login.py", "def login(user, password):\n    verify_password(user, password)\n")
    _write(tmp_path, "audit/log.py", "def write_audit_log(event):\n    dolt_commit(event)\n")
    _write(tmp_path, "ui/button.tsx", "export const Button = () => <button>click</button>\n")
    # Should be skipped:
    _write(tmp_path, ".git/HEAD", "ref: refs/heads/main\n")
    _write(tmp_path, "__pycache__/x.cpython-312.pyc", "binary garbage")
    _write(tmp_path, "node_modules/foo/index.js", "module.exports = {}\n")
    return tmp_path


def test_bm25_ranks_matching_file_first(fixture_repo: Path):
    index = build_index(fixture_repo)
    results = search(index, query="audit log dolt", top_k=3, mode="bm25")
    assert results, "expected at least one result"
    assert results[0].file == "audit/log.py", (
        f"expected audit/log.py to rank first, got {[r.file for r in results]}"
    )
    assert results[0].score > 0


def test_walker_skips_vcs_and_cache_dirs(fixture_repo: Path):
    index = build_index(fixture_repo)
    files = {chunk.file for chunk in index.chunks}
    assert ".git/HEAD" not in files
    assert not any(f.startswith("__pycache__/") for f in files)
    assert not any(f.startswith("node_modules/") for f in files)
    assert "auth/login.py" in files
    assert "audit/log.py" in files


def test_search_returns_empty_on_no_match(fixture_repo: Path):
    index = build_index(fixture_repo)
    results = search(index, query="zzzzzz_unmatchable_token_xyz", top_k=3, mode="bm25")
    assert results == []
