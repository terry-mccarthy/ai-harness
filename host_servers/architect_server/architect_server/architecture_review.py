"""``architecture_review`` — score a diff or codebase against stated invariants.

Loads ``<repo>/ARCHITECTURE.md`` plus every ``<repo>/docs/adr/*.md`` (via
:mod:`architect_server.adr`), combines them with the target payload (diff text
or codebase file tree), asks an injected ``LLMClient`` for a JSON verdict, and
returns structured findings.

The LLM is injected so unit tests can stub it; the production wiring in
:mod:`architect_server.server` passes an :class:`OllamaLLM`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Protocol

from architect_server.adr import read_adr

_VALID_MODES = ("codebase", "diff")
_MAX_CODEBASE_FILES = 200
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# Resolve repo root from this file: architect_server/architect_server/x.py → parents[3] = repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", str(_REPO_ROOT / "prompts")))
_PROMPT_PATH = _PROMPTS_DIR / "architecture_review.md"

_SYSTEM_PROMPT_CACHE: str | None = None


class LLMClient(Protocol):
    def chat(self, system: str, user: str) -> str: ...


def _system_prompt() -> str:
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        _SYSTEM_PROMPT_CACHE = _PROMPT_PATH.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT_CACHE


def load_invariants(repo: Path | str) -> dict:
    """Return ``{"architecture_md": <text>, "adrs": [{id, title, content}, ...]}``."""
    root = Path(repo)
    arch_path = root / "ARCHITECTURE.md"
    architecture_md = arch_path.read_text(encoding="utf-8") if arch_path.is_file() else ""
    adrs = read_adr(root, query=None, path=None, top_k=1000)
    return {"architecture_md": architecture_md, "adrs": adrs}


def _summarise_codebase(repo: Path) -> str:
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build"}
    files: list[str] = []
    for path in sorted(repo.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(repo)
        if any(part in skip for part in rel.parts):
            continue
        files.append(rel.as_posix())
        if len(files) >= _MAX_CODEBASE_FILES:
            break
    return "\n".join(files)


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
    """Strip ``<think>`` blocks and pull the first ``{...}`` block out of LLM output."""
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


def architecture_review(
    repo: str,
    target_mode: str,
    diff: str | None,
    llm: LLMClient,
) -> dict:
    """Score a diff or codebase against the repo's stated architectural invariants."""
    if target_mode not in _VALID_MODES:
        raise ValueError(
            f"target_mode {target_mode!r} not supported; expected one of {_VALID_MODES}"
        )
    if target_mode == "diff" and not diff:
        raise ValueError("target_mode='diff' requires a non-empty 'diff' argument")

    repo_path = Path(repo).resolve()
    invariants = load_invariants(repo_path)
    target_payload = diff if target_mode == "diff" else _summarise_codebase(repo_path)
    user_msg = _build_user_message(target_mode, invariants, target_payload)
    raw = llm.chat(system=_system_prompt(), user=user_msg)

    parsed = _extract_json(raw)
    if parsed is None:
        return {
            "target_mode": target_mode,
            "repo": str(repo_path),
            "findings": [],
            "summary": "",
            "parse_error": "LLM response was not valid JSON",
            "raw": raw[:500],
        }

    return {
        "target_mode": target_mode,
        "repo": str(repo_path),
        "findings": parsed.get("findings", []),
        "summary": parsed.get("summary", ""),
    }
