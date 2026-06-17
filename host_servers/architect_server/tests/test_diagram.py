"""Unit tests for ``diagram_gen`` — Mermaid output from a textual description.

LLM calls are stubbed so the suite stays offline.
"""
from __future__ import annotations

import pytest

from architect_server.diagram import diagram_gen


class _StubLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        return self.response


def test_returns_mermaid_for_simple_description():
    llm = _StubLLM("graph TD\n    A --> B\n")
    result = diagram_gen("A points to B", llm=llm)
    assert result["description"] == "A points to B"
    assert result["mermaid"].startswith("graph TD")
    assert "A --> B" in result["mermaid"]


def test_strips_mermaid_code_fence():
    llm = _StubLLM("```mermaid\nsequenceDiagram\n    A->>B: hi\n```\n")
    result = diagram_gen("seq", llm=llm)
    assert result["mermaid"].startswith("sequenceDiagram")
    assert "```" not in result["mermaid"]


def test_strips_bare_code_fence():
    llm = _StubLLM("```\nflowchart LR\n    X --> Y\n```")
    result = diagram_gen("flow", llm=llm)
    assert result["mermaid"].startswith("flowchart LR")
    assert "```" not in result["mermaid"]


def test_strips_thinking_block():
    llm = _StubLLM("<think>let me think about this</think>\nflowchart LR\n  X --> Y")
    result = diagram_gen("flow", llm=llm)
    assert result["mermaid"].startswith("flowchart LR")


def test_returns_parse_error_when_llm_returns_prose():
    llm = _StubLLM("Sure! Here is a diagram for you to consider.")
    result = diagram_gen("anything", llm=llm)
    assert result["mermaid"] == ""
    assert "parse_error" in result
    assert "raw" in result


def test_rejects_empty_description():
    with pytest.raises(ValueError, match="description"):
        diagram_gen("", llm=_StubLLM(""))


def test_rejects_whitespace_only_description():
    with pytest.raises(ValueError, match="description"):
        diagram_gen("   \n  ", llm=_StubLLM(""))


def test_description_is_passed_to_llm():
    llm = _StubLLM("graph TD\n  A")
    diagram_gen("auth login flow", llm=llm)
    assert llm.calls[0]["user"] == "auth login flow"


def test_accepts_all_supported_mermaid_headers():
    """Any Mermaid diagram-type header on line 1 is recognised."""
    for header in (
        "flowchart TD",
        "sequenceDiagram",
        "classDiagram",
        "stateDiagram-v2",
        "erDiagram",
        "C4Context",
        "mindmap",
    ):
        llm = _StubLLM(f"{header}\n  body line\n")
        result = diagram_gen("x", llm=llm)
        assert result["mermaid"].startswith(header), header
