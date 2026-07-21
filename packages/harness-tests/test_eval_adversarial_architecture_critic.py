"""Eval suite: score AdversarialArchitectureCritic against trap fixtures with answer keys.

Run with:  pytest -m eval -v
These tests hit real Ollama — they are slow and NOT part of the integration suite.

Each fixture pairs grounding context (canned codebase_search/adr_read/codebase_hotspots
responses) with a simulated first-pass ArchitectAgent synthesis output, plus a label
listing:
  - critic_must_confirm: findings the critic must confirm/escalate as HIGH+ with a
    concrete regression_scenario (a structural regression the first pass under-rated)
  - critic_must_refute:  findings the critic must refute/downgrade (a first-pass
    false positive it should fail to construct a working regression scenario for)

Pass bar: confirm-rate >= 0.80, refute-rate >= 0.60 (coarse — tune as fixtures grow).
"""
import json
import re
from pathlib import Path

import pytest

from harness_agents.adversarial_architecture_critic import AdversarialArchitectureCritic
from harness_agents.llm import build_llm_from_env

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "eval-fixtures" / "architecture" / "critic"
CONTEXT_DIR = FIXTURES_DIR / "context"
FIRST_PASS_DIR = FIXTURES_DIR / "first_pass"
LABELS_DIR = FIXTURES_DIR / "labels"


# ---------------------------------------------------------------------------
# Mock gateway — feeds the fixture's canned tool responses to the critic
# without a live stack
# ---------------------------------------------------------------------------

class _MockGateway:
    def __init__(self, context_path: Path):
        self._bundle = json.loads(context_path.read_text())

    async def call_tool(self, name: str, params: dict) -> dict:
        if name == "codebase_search":
            return self._bundle.get("codebase_search", {})
        if name == "adr_read":
            return self._bundle.get("adr_read", {})
        if name == "codebase_hotspots":
            return self._bundle.get("codebase_hotspots", [])
        return {}


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _pattern_matched(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text, re.IGNORECASE))


def _confirmed_findings(output: dict) -> list[dict]:
    return [f for f in output.get("findings", []) if f.get("outcome") in ("confirmed", "escalated")]


def _refuted_findings(output: dict) -> list[dict]:
    return [f for f in output.get("findings", []) if f.get("outcome") in ("refuted", "downgraded")]


def _matches_confirm_expectation(expectation: dict, findings: list[dict]) -> bool:
    for f in findings:
        if f.get("location") != expectation.get("location"):
            continue
        text = f.get("message", "") + " " + f.get("regression_scenario", "")
        if _pattern_matched(expectation["pattern"], text) and f.get("regression_scenario"):
            return True
    return False


def _matches_refute_expectation(expectation: dict, findings: list[dict]) -> bool:
    return any(f.get("location") == expectation.get("location") for f in findings)


def _score_fixture(label: dict, output: dict) -> dict:
    confirmed = _confirmed_findings(output)
    refuted = _refuted_findings(output)

    must_confirm = label.get("critic_must_confirm", [])
    must_refute = label.get("critic_must_refute", [])

    confirm_hits = sum(1 for e in must_confirm if _matches_confirm_expectation(e, confirmed))
    refute_hits = sum(1 for e in must_refute if _matches_refute_expectation(e, refuted))

    confirm_rate = confirm_hits / len(must_confirm) if must_confirm else 1.0
    refute_rate = refute_hits / len(must_refute) if must_refute else 1.0

    return {
        "confirm_rate": confirm_rate,
        "refute_rate": refute_rate,
        "confirm_hits": confirm_hits,
        "must_confirm_count": len(must_confirm),
        "refute_hits": refute_hits,
        "must_refute_count": len(must_refute),
    }


# ---------------------------------------------------------------------------
# Parametrized fixtures
# ---------------------------------------------------------------------------

def _load_fixtures():
    pairs = []
    for label_path in sorted(LABELS_DIR.glob("*.json")):
        context_path = CONTEXT_DIR / label_path.name
        first_pass_path = FIRST_PASS_DIR / label_path.name
        if context_path.exists() and first_pass_path.exists():
            pairs.append(pytest.param(context_path, first_pass_path, label_path, id=label_path.stem))
    return pairs


async def _run_fixture(context_path: Path, first_pass_path: Path, label_path: Path, llm) -> tuple[dict, dict]:
    first_pass_output = json.loads(first_pass_path.read_text())
    label = json.loads(label_path.read_text())
    repo = json.loads(context_path.read_text()).get("codebase_search", {}).get("repo", "https://github.com/example/fixture")

    agent = AdversarialArchitectureCritic(gateway=_MockGateway(context_path), llm_provider=llm, repo=repo)
    state = {
        "task": "Attack the first-pass architecture findings",
        "first_pass_output": first_pass_output,
        "thread_id": "eval",
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
    }
    result = await agent.run(state)
    assert result.get("error") is None, f"Agent returned error: {result.get('error')}"
    return label, result["agent_output"]


@pytest.mark.eval
@pytest.mark.live
@pytest.mark.parametrize("context_path,first_pass_path,label_path", _load_fixtures())
async def test_adversarial_architecture_critic_fixture(context_path: Path, first_pass_path: Path, label_path: Path):
    llm = build_llm_from_env()
    label, output = await _run_fixture(context_path, first_pass_path, label_path, llm)
    score = _score_fixture(label, output)

    print(f"\n  [{label_path.stem}]")
    print(f"  confirm: {score['confirm_hits']}/{score['must_confirm_count']}")
    print(f"  refute:  {score['refute_hits']}/{score['must_refute_count']}")
    print(f"  summary: {output.get('summary', '')[:120]}")

    if label.get("critic_must_confirm"):
        assert score["confirm_rate"] >= 0.80, (
            f"Confirm rate too low ({score['confirm_rate']:.0%})\n"
            f"Findings: {json.dumps(output.get('findings', []), indent=2)}"
        )
    if label.get("critic_must_refute"):
        assert score["refute_rate"] >= 0.60, (
            f"Refute rate too low ({score['refute_rate']:.0%})\n"
            f"Findings: {json.dumps(output.get('findings', []), indent=2)}"
        )


# ---------------------------------------------------------------------------
# Aggregate score report across all fixtures
# ---------------------------------------------------------------------------

async def _score_all_fixtures(llm) -> list[dict]:
    scores = []
    for label_path in sorted(LABELS_DIR.glob("*.json")):
        context_path = CONTEXT_DIR / label_path.name
        first_pass_path = FIRST_PASS_DIR / label_path.name
        if not (context_path.exists() and first_pass_path.exists()):
            continue
        label, output = await _run_fixture(context_path, first_pass_path, label_path, llm)
        scores.append(_score_fixture(label, output))
    return scores


@pytest.mark.eval
@pytest.mark.live
async def test_adversarial_architecture_critic_aggregate_scores():
    llm = build_llm_from_env()
    scores = await _score_all_fixtures(llm)

    confirm_scores = [s["confirm_rate"] for s in scores if s["must_confirm_count"] > 0]
    refute_scores = [s["refute_rate"] for s in scores if s["must_refute_count"] > 0]
    avg_confirm = sum(confirm_scores) / len(confirm_scores) if confirm_scores else 1.0
    avg_refute = sum(refute_scores) / len(refute_scores) if refute_scores else 1.0

    print(f"\n  Fixtures scored: {len(scores)}")
    print(f"  Avg confirm rate: {avg_confirm:.0%}  (pass bar: 80%)")
    print(f"  Avg refute rate:  {avg_refute:.0%}  (pass bar: 60%)")

    assert avg_confirm >= 0.80
    assert avg_refute >= 0.60
