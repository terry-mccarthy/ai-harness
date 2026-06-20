"""Code-forensics-style analysis for the review server.

Uses radon for cyclomatic complexity and the GitHub API for fetching
source files — no local git checkout required.

Tools:
  - code_health_score  — complexity/health scores for specific files
  - codebase_hotspots  — complexity-ranked file hotspots for a repo
  - logical_coupling   — file-level co-change analysis via commits API
"""

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

import httpx
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

_GH_HEADERS = {
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "friday-review-server",
}
if GITHUB_TOKEN:
    _GH_HEADERS["Authorization"] = f"Bearer {GITHUB_TOKEN}"


# ---------------------------------------------------------------------------
# GitHub helpers (mirrors pattern in architecture_review.py)
# ---------------------------------------------------------------------------

def _parse_github_url(url: str) -> tuple[str, str, str | None]:
    """Extract owner, repo, and optional ref from a GitHub URL.

    Supports:
      https://github.com/owner/repo
      https://github.com/owner/repo/tree/branch
      https://github.com/owner/repo.git
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip(".git").rstrip("/")
    parts = path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Not a GitHub URL: {url!r}")
    owner = parts[0]
    repo = parts[1]
    ref: str | None = None
    if len(parts) >= 4 and parts[2] == "tree":
        ref = parts[3]
    return owner, repo, ref


async def _gh_get(client: httpx.AsyncClient, path: str) -> dict | list:
    resp = await client.get(f"https://api.github.com{path}", headers=_GH_HEADERS)
    resp.raise_for_status()
    return resp.json()


async def _gh_get_text(client: httpx.AsyncClient, path: str) -> str:
    url = f"https://api.github.com{path}"
    headers = {**_GH_HEADERS, "Accept": "application/vnd.github.v3.raw"}
    resp = await client.get(url, headers=headers)
    resp.raise_for_status()
    return resp.text


async def _resolve_branch(client: httpx.AsyncClient, owner: str, repo: str, ref: str | None) -> str:
    """Return the default branch if ref is None, otherwise return ref as-is."""
    if ref:
        return ref
    try:
        repo_info = await _gh_get(client, f"/repos/{owner}/{repo}")
        if isinstance(repo_info, dict):
            return repo_info.get("default_branch", "main")
    except Exception:
        pass
    return "main"


def _extensions_for_lang(language: str | None) -> list[str]:
    lang_map = {
        "python": [".py"],
        "typescript": [".ts", ".tsx"],
        "javascript": [".js", ".jsx", ".mjs"],
        "go": [".go"],
        "rust": [".rs"],
        "java": [".java"],
        "ruby": [".rb"],
        "php": [".php"],
    }
    if language:
        return lang_map.get(language.lower(), [".py"])
    return [".py", ".ts", ".tsx", ".js", ".go", ".rs", ".java", ".rb", ".php"]


# ---------------------------------------------------------------------------
# Complexity helpers (radon-based)
# ---------------------------------------------------------------------------

def _hotspot_sort_key(r: dict) -> float:
    return r["hotspot_score"]


_CC_SCORE_TABLE = [(5, 10), (10, 8), (15, 6), (20, 4), (30, 2)]


def _complexity_score(cc: int) -> int:
    for threshold, score in _CC_SCORE_TABLE:
        if cc <= threshold:
            return score
    return 1


def _load_radon():
    """Lazy-import radon; returns (cc_visit, analyze) or None."""
    try:
        from radon.complexity import cc_visit
        from radon.raw import analyze
        return cc_visit, analyze
    except ImportError:
        return None, None


def _build_function_list(blocks) -> list[dict]:
    functions = []
    complexities = []
    for b in blocks:
        c = getattr(b, "cyclomatic_complexity", 1)
        complexities.append(c)
        functions.append({
            "name": getattr(b, "name", "<anonymous>"),
            "type": getattr(b, "type", "function"),
            "lineno": getattr(b, "lineno", 0),
            "cyclomatic_complexity": c,
            "score": _complexity_score(c),
        })
    return functions, complexities


def _file_health_score(source: str) -> dict[str, Any]:
    cc_visit, analyze = _load_radon()
    if cc_visit is None:
        return _fallback_health(source)

    try:
        blocks = cc_visit(source)
        raw = analyze(source)
    except Exception:
        return _fallback_health(source)

    functions, complexities = _build_function_list(blocks)

    if not complexities:
        return {
            "score": 10,
            "nloc": raw.loc if raw else 0,
            "functions": [],
            "note": "no callable blocks found",
        }

    avg_cc = sum(complexities) / len(complexities)
    max_cc = max(complexities)

    return {
        "score": _complexity_score(max_cc),
        "nloc": raw.loc if raw else 0,
        "avg_cyclomatic_complexity": round(avg_cc, 1),
        "max_cyclomatic_complexity": max_cc,
        "functions": functions,
    }


_SCORE_BY_LOC = [
    (0, 10),
    (50, 9),
    (200, 7),
    (500, 5),
]


def _is_comment_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    return stripped.startswith(("#", "//", "/*", "*", "/*"))


def _loc_score(loc: int) -> int:
    for threshold, score in _SCORE_BY_LOC:
        if loc <= threshold:
            return score
    return 3


def _fallback_health(source: str) -> dict[str, Any]:
    lines = source.splitlines()
    loc = len([l for l in lines if not _is_comment_line(l)])
    return {"score": _loc_score(loc), "nloc": loc, "functions": []}


def _hotspot_rank(health: dict) -> float:
    """Score a file for hotspot ranking (lower = healthier)."""
    return 10 - health.get("score", 5)


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------

async def _fetch_and_score(client: httpx.AsyncClient, api_path: str, file_path: str) -> dict[str, Any]:
    """Fetch a file from the GitHub API and return its health score."""
    try:
        content = await _gh_get_text(client, api_path)
        health = _file_health_score(content)
        return {"file": file_path, **health}
    except httpx.HTTPStatusError as e:
        return {"file": file_path, "score": None, "error": f"HTTP {e.response.status_code}"}
    except Exception as e:
        return {"file": file_path, "score": None, "error": str(e)}


async def get_code_health(
    file_paths: list[str],
    repo: str,
) -> list[dict[str, Any]]:
    """Fetch each file from the GitHub repo and score its code health.

    Returns a list of per-file results sorted worst-first (lowest score).
    """
    owner, repo_name, ref = _parse_github_url(repo)
    results: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=30) as client:
        branch = await _resolve_branch(client, owner, repo_name, ref)
        tasks = []
        for fp in file_paths:
            path = f"/repos/{owner}/{repo_name}/contents/{fp.lstrip('/')}?ref={branch}"
            tasks.append(_fetch_and_score(client, path, fp))
        completed = await asyncio.gather(*tasks, return_exceptions=True)

    for fp, result in zip(file_paths, completed):
        if isinstance(result, Exception):
            results.append({"file": fp, "score": None, "error": str(result)})
        else:
            results.append(result)

    results.sort(key=lambda r: r.get("score") or 0)
    return results


async def _fetch_file_tree(
    client: httpx.AsyncClient, owner: str, repo_name: str, branch: str
) -> tuple[dict | None, str | None]:
    try:
        tree_data = await _gh_get(client, f"/repos/{owner}/{repo_name}/git/trees/{branch}?recursive=1")
        return tree_data, None
    except Exception as e:
        return None, f"Cannot fetch file tree for branch '{branch}': {e}"


def _matches_ext(path: str, extensions: list[str]) -> bool:
    for ext in extensions:
        if path.endswith(ext):
            return True
    return False


def _is_source_file(item: dict, extensions: list[str]) -> bool:
    return item.get("type") == "blob" and _matches_ext(item["path"], extensions)


def _filter_source_files(tree_data: dict, extensions: list[str]) -> list[str]:
    if not isinstance(tree_data, dict):
        return []
    tree = tree_data.get("tree")
    if not tree:
        return []
    return [item["path"] for item in tree if _is_source_file(item, extensions)]


async def _score_file(
    client: httpx.AsyncClient, owner: str, repo_name: str, branch: str, fp: str
) -> dict | None:
    try:
        content = await _gh_get_text(client, f"/repos/{owner}/{repo_name}/contents/{fp}?ref={branch}")
        health = _file_health_score(content)
        return {
            "file": fp,
            "hotspot_score": round(_hotspot_rank(health), 2),
            "complexity_score": health.get("max_cyclomatic_complexity", 0),
            "nloc": health.get("nloc", 0),
        }
    except Exception:
        return None


async def get_hotspots(
    repo: str,
    top_n: int = 10,
    language: str | None = None,
) -> list[dict[str, Any]]:
    owner, repo_name, ref = _parse_github_url(repo)
    extensions = _extensions_for_lang(language)

    async with httpx.AsyncClient(timeout=30) as client:
        branch = await _resolve_branch(client, owner, repo_name, ref)
        tree_data, error = await _fetch_file_tree(client, owner, repo_name, branch)
        if error:
            return [{"error": error}]

        files = _filter_source_files(tree_data, extensions)

        scored: list[dict] = []
        for fp in files[:top_n * 3]:
            score = await _score_file(client, owner, repo_name, branch, fp)
            if score is not None:
                scored.append(score)

    scored.sort(key=_hotspot_sort_key, reverse=True)
    return scored[:top_n]


async def _fetch_commit_page(
    client: httpx.AsyncClient,
    owner: str,
    repo_name: str,
    file_path: str,
    branch: str,
    page: int,
) -> list[dict]:
    """Fetch a page of commits. Returns empty list on error or no more commits."""
    try:
        commits = await _gh_get(
            client,
            f"/repos/{owner}/{repo_name}/commits?path={file_path}&per_page=30&page={page}&sha={branch}",
        )
    except Exception:
        return []
    if not commits:
        return []
    if isinstance(commits, dict):
        return [commits]
    return commits


async def _get_changed_files(
    client: httpx.AsyncClient,
    owner: str,
    repo_name: str,
    commit: dict,
    file_path: str,
) -> list[str]:
    sha = commit.get("sha", "")
    if not sha:
        return []
    try:
        detail = await _gh_get(client, f"/repos/{owner}/{repo_name}/commits/{sha}")
    except Exception:
        return []
    return _other_files(detail.get("files") or [], file_path)


def _other_files(files: list[dict], file_path: str) -> list[str]:
    result = []
    for f in files:
        if f["filename"] != file_path:
            result.append(f["filename"])
    return result


def _update_coupling(coupling: dict[str, int], changed: list[str]) -> None:
    for cf in set(changed):
        coupling[cf] = coupling.get(cf, 0) + 1


def _to_coupling_result(coupling: dict[str, int]) -> list[dict[str, Any]]:
    sorted_files = sorted(coupling.items(), key=lambda x: -x[1])
    result = []
    for cf, count in sorted_files[:20]:
        result.append({"file": cf, "co_change_count": count})
    return result


async def get_logical_coupling(
    repo: str,
    file_path: str,
    max_commits: int = 50,
) -> list[dict[str, Any]]:
    """Find files that historically co-change with the given file.

    Uses the GitHub commits API to find recent commits touching
    ``file_path``, then extracts all other files changed in those
    commits. Returns co-changing files ranked by frequency.
    """
    owner, repo_name, ref = _parse_github_url(repo)
    coupling: dict[str, int] = {}

    async with httpx.AsyncClient(timeout=30) as client:
        branch = await _resolve_branch(client, owner, repo_name, ref)
        page = 1
        fetched = 0

        while fetched < max_commits:
            commits = await _fetch_commit_page(client, owner, repo_name, file_path, branch, page)
            if not commits:
                break

            remaining = max_commits - fetched
            for commit in commits[:remaining]:
                fetched += 1
                changed = await _get_changed_files(client, owner, repo_name, commit, file_path)
                _update_coupling(coupling, changed)

            if len(commits) < 30:
                break
            page += 1

    return _to_coupling_result(coupling)
