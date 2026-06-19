"""``architecture_review`` for the review server.

Fetches ARCHITECTURE.md + ADRs from a GitHub repo (via raw.githubusercontent.com
and the GitHub API), builds a prompt, calls the review server's LLM provider,
and returns structured findings.
"""
import json
import logging
import os
import re
from pathlib import Path
from typing import List, Dict
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_MAX_CODEBASE_FILES = 200
_VALID_MODES = ("codebase", "diff")
_GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")

_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", str(Path(__file__).resolve().parent / "prompts")))
_PROMPT_PATH = _PROMPTS_DIR / "architecture_review.md"

_SYSTEM_PROMPT_CACHE: str | None = None


def _system_prompt() -> str:
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        _SYSTEM_PROMPT_CACHE = _PROMPT_PATH.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT_CACHE


def _parse_github_url(url: str) -> tuple[str, str, str]:
    """Extract owner, repo, and ref from a GitHub URL.

    Supports:
      https://github.com/owner/repo
      https://github.com/owner/repo/tree/branch
      https://github.com/owner/repo.git
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip(".git").rstrip("/")
    parts = path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Invalid GitHub URL: {url!r}")
    owner = parts[0]
    repo = parts[1]
    ref = "main"
    if len(parts) >= 4 and parts[2] == "tree":
        ref = parts[3]
    return owner, repo, ref


def _gh_headers() -> dict:
    headers = {"Accept": "application/vnd.github.v3+json"}
    if _GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {_GITHUB_TOKEN}"
    return headers


async def _fetch_text(url: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=_gh_headers(), timeout=30.0)
        resp.raise_for_status()
        return resp.text


async def _fetch_architecture_md(owner: str, repo: str, ref: str) -> str:
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/ARCHITECTURE.md"
    try:
        return await _fetch_text(url)
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return ""
        raise


async def _list_adr_files(owner: str, repo: str, ref: str) -> list[str]:
    """List docs/adr/*.md files in the repo via the GitHub API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/docs/adr?ref={ref}"
    try:
        text = await _fetch_text(url)
        items = json.loads(text)
        return [
            item["name"]
            for item in items
            if item["type"] == "file" and item["name"].endswith(".md")
        ]
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 403):
            return []
        raise


async def _fetch_adr(owner: str, repo: str, ref: str, name: str) -> dict | None:
    """Fetch a single ADR file and return {id, title, content}."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/docs/adr/{name}"
    try:
        content = await _fetch_text(url)
    except httpx.HTTPStatusError:
        return None
    adr_id = name.split("-", 1)[0] if re.match(r"^\d{4}", name) else name
    title = name.replace(".md", "").split("-", 1)[1] if "-" in name else name.replace(".md", "")
    return {"id": adr_id, "title": title.replace("-", " ").title(), "content": content}


async def _fetch_invariants(owner: str, repo: str, ref: str) -> dict:
    """Return {"architecture_md": ..., "adrs": [...]}."""
    architecture_md = await _fetch_architecture_md(owner, repo, ref)
    adr_files = await _list_adr_files(owner, repo, ref)
    adrs: list[dict] = []
    for fname in sorted(adr_files):
        adr = await _fetch_adr(owner, repo, ref, fname)
        if adr:
            adrs.append(adr)
    return {"architecture_md": architecture_md, "adrs": adrs}


async def _fetch_file_tree(owner: str, repo: str, ref: str) -> str:
    """Fetch the repo file tree via the GitHub Git Trees API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/git/trees/{ref}?recursive=1"
    try:
        text = await _fetch_text(url)
        data = json.loads(text)
        skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
        files: list[str] = []
        for item in data.get("tree", []):
            if item["type"] != "blob":
                continue
            path = item["path"]
            if any(part in skip for part in Path(path).parts):
                continue
            files.append(path)
            if len(files) >= _MAX_CODEBASE_FILES:
                break
        return "\n".join(sorted(files))
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (404, 403):
            return "(unable to fetch file tree — API rate limit or private repo)"
        raise


def _format_invariants(inv: dict) -> str:
    parts: list[str] = []
    if inv["architecture_md"]:
        parts.append("=== ARCHITECTURE.md ===\n" + inv["architecture_md"])
    else:
        parts.append("=== ARCHITECTURE.md ===\n(no ARCHITECTURE.md found)")
    if inv["adrs"]:
        parts.append("=== ADRs ===")
        for adr in inv["adrs"]:
            parts.append(
                f"--- ADR-{adr['id']} {adr['title']} (status: {adr.get('status', 'unknown')}) ---\n"
                + adr["content"]
            )
    else:
        parts.append("=== ADRs ===\n(no ADRs found)")
    return "\n\n".join(parts)


def _build_user_message(target_mode: str, invariants: dict, target_payload: str) -> str:
    has_anything = invariants["architecture_md"] or invariants["adrs"]
    preamble = (
        f"target_mode: {target_mode}\n\n"
        + (
            "Invariants follow.\n\n"
            if has_anything
            else "No invariants are stated for this repo (no ARCHITECTURE.md, no ADRs).\n\n"
        )
        + _format_invariants(invariants)
    )
    target_header = "=== Diff ===" if target_mode == "diff" else "=== Codebase file tree ==="
    return f"{preamble}\n\n{target_header}\n{target_payload}\n"


def _extract_json(text: str) -> dict | None:
    cleaned = _THINK_RE.sub("", text).strip()
    if not cleaned:
        return None
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None


async def architecture_review(
    repo: str,
    target_mode: str,
    diff: str | None,
    llm_provider,
) -> dict:
    """Score a diff or codebase against the repo's stated architectural invariants.

    Fetches invariants (ARCHITECTURE.md + ADRs) from the GitHub repo.
    ``llm_provider`` must have ``async chat(messages) -> LLMResponse``.
    """
    if target_mode not in _VALID_MODES:
        raise ValueError(
            f"target_mode {target_mode!r} not supported; expected one of {_VALID_MODES}"
        )
    if target_mode == "diff" and not diff:
        raise ValueError("target_mode='diff' requires a non-empty 'diff' argument")

    owner, repo_name, ref = _parse_github_url(repo)
    invariants = await _fetch_invariants(owner, repo_name, ref)

    if target_mode == "diff":
        target_payload = diff
    else:
        target_payload = await _fetch_file_tree(owner, repo_name, ref)

    user_msg = _build_user_message(target_mode, invariants, target_payload)

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": _system_prompt()},
        {"role": "user", "content": user_msg},
    ]
    response = await llm_provider.chat(messages)
    raw = response.content

    parsed = _extract_json(raw)
    if parsed is None:
        return {
            "target_mode": target_mode,
            "repo": repo,
            "findings": [],
            "summary": "",
            "parse_error": "LLM response was not valid JSON",
            "raw": raw[:500],
        }

    return {
        "target_mode": target_mode,
        "repo": repo,
        "findings": parsed.get("findings", []),
        "summary": parsed.get("summary", ""),
    }
