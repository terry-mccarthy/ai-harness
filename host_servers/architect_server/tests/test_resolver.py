"""Tests for the git-URL repo resolver.

Uses ``file://`` URLs against a tmp-dir git repo so the suite stays offline.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from architect_server.resolver import (
    ResolvedRepo,
    is_git_url,
    parse_repo,
    resolve_git_repo,
)


def _git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return r.stdout


def _init_repo(root: Path, files: dict[str, str]) -> str:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return _git(root, "rev-parse", "HEAD").strip()


def test_is_git_url_recognises_http_https_file():
    assert is_git_url("https://github.com/foo/bar")
    assert is_git_url("http://gitea.local/foo/bar")
    assert is_git_url("file:///tmp/foo")
    assert is_git_url("https://github.com/foo/bar@v1.0")
    assert not is_git_url("/local/path")
    assert not is_git_url("./relative")
    assert not is_git_url("relative/path")


def test_parse_repo_extracts_url_and_ref():
    assert parse_repo("https://github.com/foo/bar@v1.0") == ("https://github.com/foo/bar", "v1.0")
    assert parse_repo("https://github.com/foo/bar") == ("https://github.com/foo/bar", None)
    assert parse_repo("file:///tmp/foo@abc1234") == ("file:///tmp/foo", "abc1234")


def test_parse_repo_ignores_at_inside_url_authority():
    """``user:tok@host`` style auth must not be misread as a ref."""
    url, ref = parse_repo("https://user:token@github.com/foo/bar")
    assert url == "https://user:token@github.com/foo/bar"
    assert ref is None


def test_resolve_git_repo_clones_and_keys_by_sha(tmp_path: Path):
    origin = tmp_path / "origin"
    origin_sha = _init_repo(origin, {"README.md": "hello\n", "src/x.py": "print('x')\n"})

    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    resolved = resolve_git_repo(f"file://{origin}", cache_root)

    assert isinstance(resolved, ResolvedRepo)
    assert resolved.cache_key == origin_sha
    assert (resolved.local_path / "README.md").read_text() == "hello\n"
    assert (resolved.local_path / "src" / "x.py").read_text() == "print('x')\n"


def test_resolve_git_repo_with_ref_checks_out_that_ref(tmp_path: Path):
    origin = tmp_path / "origin"
    v1_sha = _init_repo(origin, {"README.md": "v1\n"})
    _git(origin, "tag", "v1.0")

    (origin / "README.md").write_text("v2\n")
    _git(origin, "commit", "-q", "-am", "v2")
    v2_sha = _git(origin, "rev-parse", "HEAD").strip()
    assert v1_sha != v2_sha

    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    resolved = resolve_git_repo(f"file://{origin}@v1.0", cache_root)
    assert resolved.cache_key == v1_sha
    assert (resolved.local_path / "README.md").read_text() == "v1\n"


def test_resolve_git_repo_reuses_cached_clone(tmp_path: Path):
    origin = tmp_path / "origin"
    origin_sha = _init_repo(origin, {"README.md": "hello\n"})
    cache_root = tmp_path / "cache"
    cache_root.mkdir()

    r1 = resolve_git_repo(f"file://{origin}", cache_root)
    r2 = resolve_git_repo(f"file://{origin}", cache_root)

    assert r1.cache_key == r2.cache_key == origin_sha
    assert r1.local_path == r2.local_path
    # Exactly one SHA-named directory under cache_root.
    sha_dirs = [d for d in cache_root.iterdir() if d.is_dir() and d.name == origin_sha]
    assert len(sha_dirs) == 1


def test_resolve_git_repo_raises_clear_error_on_clone_failure(tmp_path: Path):
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    with pytest.raises(RuntimeError, match="clone"):
        resolve_git_repo(f"file://{tmp_path / 'does-not-exist'}", cache_root)
