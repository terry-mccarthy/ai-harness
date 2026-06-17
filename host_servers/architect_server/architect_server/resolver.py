"""Resolve a ``repo`` reference to a local directory + cache key.

Two schemes for v1:

- Local path: ``/abs/path`` or ``./relative`` — cache key is the resolved absolute path.
- Git URL: ``https://``, ``http://``, ``file://`` (with optional ``@<ref>``) — shallow-cloned
  into ``<cache_root>/<sha>``; cache key is the commit SHA.

The clone cache is on disk so reruns against the same SHA short-circuit. The
in-memory ``IndexCache`` then memoises the parsed/embedded ``Index`` per SHA.
"""
from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

_GIT_URL_PREFIXES = ("http://", "https://", "file://")


@dataclass(frozen=True)
class ResolvedRepo:
    cache_key: str
    local_path: Path


def is_git_url(repo: str) -> bool:
    return repo.startswith(_GIT_URL_PREFIXES)


def parse_repo(repo: str) -> tuple[str, str | None]:
    """Split an optional ``@<ref>`` suffix from a repo reference.

    A ref must not contain ``/`` so that ``user:tok@host/path`` style URL
    authority is not misread as a ref.
    """
    if "@" not in repo:
        return repo, None
    url, ref = repo.rsplit("@", 1)
    if "/" in ref or not ref:
        return repo, None
    return url, ref


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def resolve_git_repo(repo: str, cache_root: Path) -> ResolvedRepo:
    """Clone a git URL (optionally at ``@<ref>``) into ``<cache_root>/<sha>``.

    Subsequent calls for the same resolved SHA reuse the on-disk clone.
    """
    cache_root.mkdir(parents=True, exist_ok=True)
    url, ref = parse_repo(repo)

    with tempfile.TemporaryDirectory(dir=str(cache_root)) as tmp:
        clone_path = Path(tmp) / "clone"
        try:
            _git("clone", "--depth", "1", "--no-tags", "--quiet", url, str(clone_path))
        except RuntimeError as exc:
            raise RuntimeError(f"failed to clone {url}: {exc}") from exc

        if ref:
            _git("fetch", "--depth", "1", "--quiet", "origin", ref, cwd=clone_path)
            _git("checkout", "--quiet", "FETCH_HEAD", cwd=clone_path)

        sha = _git("rev-parse", "HEAD", cwd=clone_path).strip()

        final = cache_root / sha
        if final.exists():
            return ResolvedRepo(cache_key=sha, local_path=final)
        clone_path.rename(final)
        return ResolvedRepo(cache_key=sha, local_path=final)
