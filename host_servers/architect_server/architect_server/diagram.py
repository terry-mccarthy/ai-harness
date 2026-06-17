"""``diagram_gen`` — turn a textual description into a Mermaid diagram via LLM.

The LLM is injected so unit tests can stub it; the production wiring in
:mod:`architect_server.server` passes an :class:`OllamaLLM`. Output is the raw
Mermaid text (no code fences); ``<think>`` blocks and stray markdown fences are
stripped before validation.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Protocol

_MERMAID_HEADERS = (
    "graph",
    "flowchart",
    "sequenceDiagram",
    "classDiagram",
    "stateDiagram-v2",
    "stateDiagram",
    "erDiagram",
    "gantt",
    "pie",
    "journey",
    "gitGraph",
    "mindmap",
    "timeline",
    "C4Context",
)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_REPO_ROOT = Path(__file__).resolve().parents[3]
_PROMPTS_DIR = Path(os.environ.get("PROMPTS_DIR", str(_REPO_ROOT / "prompts")))
_PROMPT_PATH = _PROMPTS_DIR / "diagram_gen.md"

_SYSTEM_PROMPT_CACHE: str | None = None


class LLMClient(Protocol):
    def chat(self, system: str, user: str) -> str: ...


def _system_prompt() -> str:
    global _SYSTEM_PROMPT_CACHE
    if _SYSTEM_PROMPT_CACHE is None:
        _SYSTEM_PROMPT_CACHE = _PROMPT_PATH.read_text(encoding="utf-8")
    return _SYSTEM_PROMPT_CACHE


def _strip_outer_fence(text: str) -> str:
    """Remove a single outer ```` ``` ```` / ```` ```mermaid ```` block if present."""
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "```":
            return "\n".join(lines[1:i]).strip()
    return "\n".join(lines[1:]).strip()


def _extract_mermaid(text: str) -> str | None:
    cleaned = _THINK_RE.sub("", text).strip()
    if not cleaned:
        return None
    candidate = _strip_outer_fence(cleaned).strip()
    if not candidate:
        return None
    first_line = candidate.split("\n", 1)[0].strip()
    if any(first_line == h or first_line.startswith(h + " ") for h in _MERMAID_HEADERS):
        return candidate
    return None


def diagram_gen(description: str, llm: LLMClient) -> dict:
    if not description.strip():
        raise ValueError("description must be non-empty")
    raw = llm.chat(system=_system_prompt(), user=description)
    mermaid = _extract_mermaid(raw)
    if mermaid is None:
        return {
            "description": description,
            "mermaid": "",
            "parse_error": "LLM output did not start with a Mermaid diagram declaration",
            "raw": raw[:500],
        }
    return {"description": description, "mermaid": mermaid}
