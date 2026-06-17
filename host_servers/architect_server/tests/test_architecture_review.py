"""Unit tests for ``architecture_review`` — repo-grounded invariant scoring.

LLM calls are mocked so the suite stays offline. A live Ollama smoke test
belongs in a separate opt-in file if/when needed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from architect_server.architecture_review import architecture_review, load_invariants


class _StubLLM:
    def __init__(self, response: str):
        self.response = response
        self.calls: list[dict] = []

    def chat(self, system: str, user: str) -> str:
        self.calls.append({"system": system, "user": user})
        return self.response


def _seed_repo(root: Path, invariants: str = "Layer rule: API must not import DB directly."):
    root.mkdir(parents=True, exist_ok=True)
    (root / "ARCHITECTURE.md").write_text(f"# Architecture\n\n{invariants}\n")
    adr_dir = root / "docs" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)
    (adr_dir / "0001-layering.md").write_text(
        "# Layering\n\n**Status:** Accepted\n\nAPI layer must not import DB layer.\n"
    )


def test_load_invariants_returns_architecture_md_and_adrs(tmp_path: Path):
    _seed_repo(tmp_path, invariants="No global state.")
    inv = load_invariants(tmp_path)
    assert "No global state" in inv["architecture_md"]
    assert len(inv["adrs"]) == 1
    assert inv["adrs"][0]["id"] == "0001"
    assert "API layer" in inv["adrs"][0]["content"]


def test_load_invariants_handles_missing_files(tmp_path: Path):
    """No ARCHITECTURE.md and no ADRs is not an error — just an empty invariant set."""
    inv = load_invariants(tmp_path)
    assert inv["architecture_md"] == ""
    assert inv["adrs"] == []


def test_invalid_target_mode_raises(tmp_path: Path):
    _seed_repo(tmp_path)
    with pytest.raises(ValueError, match="target_mode"):
        architecture_review(
            repo=str(tmp_path), target_mode="other", diff=None, llm=_StubLLM("{}")
        )


def test_diff_mode_requires_diff_text(tmp_path: Path):
    _seed_repo(tmp_path)
    with pytest.raises(ValueError, match="diff"):
        architecture_review(
            repo=str(tmp_path), target_mode="diff", diff=None, llm=_StubLLM("{}")
        )


def test_diff_mode_passes_diff_and_invariants_to_llm(tmp_path: Path):
    _seed_repo(tmp_path, invariants="API must not import DB.")
    llm = _StubLLM(
        json.dumps(
            {
                "findings": [
                    {
                        "severity": "HIGH",
                        "rule": "ADR-0001 layering",
                        "title": "API imports DB",
                        "message": "src/api/h.py now imports from db.session",
                        "location": "src/api/h.py:1",
                    }
                ],
                "summary": "One layering violation introduced.",
            }
        )
    )

    result = architecture_review(
        repo=str(tmp_path),
        target_mode="diff",
        diff="+from db import session\n",
        llm=llm,
    )

    assert result["target_mode"] == "diff"
    assert result["repo"] == str(Path(tmp_path).resolve())
    assert len(result["findings"]) == 1
    assert result["findings"][0]["severity"] == "HIGH"
    assert result["summary"] == "One layering violation introduced."

    sent = llm.calls[0]["user"]
    assert "from db import session" in sent
    assert "API must not import DB" in sent
    assert "0001-layering" in sent or "Layering" in sent


def test_codebase_mode_includes_file_tree_in_prompt(tmp_path: Path):
    _seed_repo(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "auth.py").write_text("def login(): ...\n")
    (tmp_path / "src" / "db.py").write_text("def query(): ...\n")

    llm = _StubLLM(json.dumps({"findings": [], "summary": "Clean."}))
    result = architecture_review(
        repo=str(tmp_path), target_mode="codebase", diff=None, llm=llm
    )

    assert result["target_mode"] == "codebase"
    assert result["findings"] == []
    sent = llm.calls[0]["user"]
    assert "src/auth.py" in sent
    assert "src/db.py" in sent


def test_thinking_blocks_stripped_before_json_parse(tmp_path: Path):
    """Some models emit ``<think>...</think>`` before the JSON answer."""
    _seed_repo(tmp_path)
    payload = json.dumps({"findings": [], "summary": "ok"})
    llm = _StubLLM(f"<think>internal reasoning</think>\n\n{payload}")
    result = architecture_review(
        repo=str(tmp_path), target_mode="diff", diff="+x\n", llm=llm
    )
    assert result["summary"] == "ok"
    assert result["findings"] == []


def test_garbage_llm_response_returns_parse_error(tmp_path: Path):
    """When the LLM returns non-JSON, surface a structured error rather than crashing."""
    _seed_repo(tmp_path)
    llm = _StubLLM("certainly not json")
    result = architecture_review(
        repo=str(tmp_path), target_mode="diff", diff="+x\n", llm=llm
    )
    assert result["target_mode"] == "diff"
    assert "parse_error" in result
    assert result["findings"] == []


def test_codebase_mode_works_without_any_invariants(tmp_path: Path):
    """No ARCHITECTURE.md, no ADRs — still runs; user prompt notes the absence."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "x.py").write_text("x = 1\n")

    llm = _StubLLM(json.dumps({"findings": [], "summary": "No invariants to check."}))
    result = architecture_review(
        repo=str(tmp_path), target_mode="codebase", diff=None, llm=llm
    )
    assert result["findings"] == []
    sent = llm.calls[0]["user"]
    assert "no invariants" in sent.lower() or "no architecture" in sent.lower()
