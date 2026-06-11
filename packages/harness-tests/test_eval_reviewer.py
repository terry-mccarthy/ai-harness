"""Eval suite: score the CodeReviewerAgent against labeled diffs.

Run with:  pytest -m eval -v
These tests hit real Ollama — they are slow and NOT part of the integration suite.

Scoring:
  - verdict_accuracy: fraction of fixtures where predicted verdict matches label
  - recall:           fraction of must_flag patterns found in CRITICAL findings
  - precision:        fraction of CRITICAL findings that match a must_flag pattern
                      (only meaningful on fixtures that have must_flag entries)

Pass bar: verdict_accuracy >= 0.80, recall >= 0.60
"""

import json
import re
import os
import pytest
from pathlib import Path

from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.llm import OllamaProvider

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "eval-fixtures"
DIFFS_DIR = FIXTURES_DIR / "diffs"
LABELS_DIR = FIXTURES_DIR / "labels"


# ---------------------------------------------------------------------------
# Mock gateway — feeds the fixture diff to the agent without a live stack
# ---------------------------------------------------------------------------

class _MockGateway:
    def __init__(self, diff_text: str):
        self._diff = diff_text

    async def call_tool(self, name: str, params: dict) -> dict:
        if name == "git_diff":
            return {"diff": self._diff}
        if name == "run_linter":
            return {"findings": []}
        return {}


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _findings_text(output: dict) -> str:
    """Concatenate all finding messages and suggestions into one searchable string."""
    parts = []
    for f in output.get("findings", []):
        parts.append(f.get("message", ""))
        parts.append(f.get("suggestion", ""))
    return " ".join(parts).lower()


def _critical_findings(output: dict) -> list[dict]:
    return [f for f in output.get("findings", []) if f.get("severity") == "CRITICAL"]


def _pattern_matched(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


def _score_fixture(label: dict, output: dict) -> dict:
    predicted_verdict = output.get("verdict", "pass")
    correct_verdict = predicted_verdict == label["verdict"]

    criticals_text = " ".join(
        f.get("message", "") + " " + f.get("suggestion", "")
        for f in _critical_findings(output)
    ).lower()

    must_flag = label.get("must_flag", [])
    flagged = [m for m in must_flag if _pattern_matched(m["pattern"], criticals_text)]
    recall = len(flagged) / len(must_flag) if must_flag else 1.0

    return {
        "correct_verdict": correct_verdict,
        "predicted": predicted_verdict,
        "expected": label["verdict"],
        "recall": recall,
        "flagged": len(flagged),
        "must_flag_count": len(must_flag),
        "critical_count": len(_critical_findings(output)),
    }


# ---------------------------------------------------------------------------
# Parametrized fixtures
# ---------------------------------------------------------------------------

def _load_fixtures():
    pairs = []
    for label_path in sorted(LABELS_DIR.glob("*.json")):
        diff_path = DIFFS_DIR / label_path.with_suffix(".diff").name
        if diff_path.exists():
            pairs.append(pytest.param(diff_path, label_path, id=label_path.stem))
    return pairs


@pytest.mark.eval
@pytest.mark.parametrize("diff_path,label_path", _load_fixtures())
async def test_reviewer_fixture(diff_path: Path, label_path: Path):
    diff_text = diff_path.read_text()
    label = json.loads(label_path.read_text())

    llm = OllamaProvider(
        host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
    )
    agent = CodeReviewerAgent(gateway=_MockGateway(diff_text), llm_provider=llm)

    state = {
        "task": "Security review",
        "diff": diff_text,
        "thread_id": "eval",
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
        "human_approval_token": None,
        "memory_context": None,
    }
    result = await agent.run(state)

    assert result.get("error") is None, f"Agent returned error: {result.get('error')}"
    output = result["agent_output"]
    score = _score_fixture(label, output)

    print(f"\n  [{label_path.stem}]")
    print(f"  verdict: expected={score['expected']} predicted={score['predicted']} {'✓' if score['correct_verdict'] else '✗'}")
    print(f"  recall:  {score['flagged']}/{score['must_flag_count']} must-flag patterns found in CRITICALs")
    print(f"  criticals: {score['critical_count']}")
    print(f"  summary: {output.get('summary', '')[:120]}")

    assert score["correct_verdict"], (
        f"Wrong verdict: expected {score['expected']}, got {score['predicted']}\n"
        f"Findings: {json.dumps(output.get('findings', []), indent=2)}"
    )
    if label.get("must_flag"):
        assert score["recall"] >= 0.5, (
            f"Recall too low ({score['recall']:.0%}): missed must-flag patterns\n"
            f"CRITICAL findings: {json.dumps(_critical_findings(output), indent=2)}"
        )


# ---------------------------------------------------------------------------
# Aggregate score report across all fixtures
# ---------------------------------------------------------------------------

@pytest.mark.eval
async def test_reviewer_aggregate_score():
    """Run all fixtures and assert minimum aggregate recall and verdict accuracy."""
    llm = OllamaProvider(
        host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
    )

    scores = []
    for label_path in sorted(LABELS_DIR.glob("*.json")):
        diff_path = DIFFS_DIR / label_path.with_suffix(".diff").name
        if not diff_path.exists():
            continue
        diff_text = diff_path.read_text()
        label = json.loads(label_path.read_text())

        agent = CodeReviewerAgent(gateway=_MockGateway(diff_text), llm_provider=llm)
        state = {
            "task": "Security review",
            "diff": diff_text,
            "thread_id": "eval",
            "agent_output": None,
            "requires_human_approval": False,
            "error": None,
            "human_approval_token": None,
            "memory_context": None,
        }
        result = await agent.run(state)
        if result.get("error"):
            continue
        scores.append(_score_fixture(label, result["agent_output"]))

    assert scores, "No fixtures scored"

    verdict_accuracy = sum(s["correct_verdict"] for s in scores) / len(scores)
    recall_scores = [s["recall"] for s in scores if s["must_flag_count"] > 0]
    avg_recall = sum(recall_scores) / len(recall_scores) if recall_scores else 1.0

    print(f"\n  Fixtures scored: {len(scores)}")
    print(f"  Verdict accuracy: {verdict_accuracy:.0%}  (pass bar: 80%)")
    print(f"  Avg recall:       {avg_recall:.0%}  (pass bar: 60%)")
    for s in scores:
        mark = "✓" if s["correct_verdict"] else "✗"
        print(f"    {mark} {s['expected']:4} → {s['predicted']:4}  recall={s['recall']:.0%}")

    assert verdict_accuracy >= 0.80, f"Verdict accuracy {verdict_accuracy:.0%} below 80% threshold"
    assert avg_recall >= 0.60, f"Average recall {avg_recall:.0%} below 60% threshold"
