"""Eval suite: score the ArchitectAgent against labeled architecture fixtures.

Run with:  pytest -m eval -v -s
These tests hit real Ollama — they are slow and NOT part of the integration suite.

Each fixture is a small "repository" expressed as canned tool responses (no live
stack, no GitHub). A _MockGateway feeds the four phases their data:

  reconnaissance      -> codebase_search("directory structure...")  + codebase_hotspots
  flow_trace          -> codebase_search("entry point...")
  abstraction_analysis-> codebase_search(<interfaces>)
  synthesis           -> adr_read

Scoring (mirrors the reviewer eval):
  - schema_valid:        synthesis output validates against ARCHITECT_OUTPUT_SCHEMA
  - detection_accuracy:  smell fixtures raise a HIGH+ finding; the control does not
                         raise a CRITICAL
  - recall:              fraction of a fixture's must_flag patterns found in HIGH+ findings

Pass bars: schema validity 100%, detection accuracy >= 0.66, avg recall >= 0.50.
These are deliberately coarse for a small live-LLM set; tune as fixtures grow.
"""

import json
import os
import re
from pathlib import Path

import jsonschema
import pytest

from harness_agents.architect import ArchitectAgent
from harness_agents.llm import OllamaProvider
from harness_agents.types import ARCHITECT_OUTPUT_SCHEMA

FIXTURES_DIR = Path(__file__).resolve().parents[2] / "eval-fixtures" / "architecture"
LABELS_DIR = FIXTURES_DIR / "labels"

_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, "CRITICAL": 4}


# ---------------------------------------------------------------------------
# Mock gateway — serves a fixture's canned tool responses to the four phases
# ---------------------------------------------------------------------------

class _MockGateway:
    def __init__(self, case_dir: Path):
        self.gateway_url = "https://github.com/example/fixture"
        self._dir = case_dir

    def _load(self, name: str):
        return json.loads((self._dir / name).read_text())

    async def call_tool(self, name: str, params: dict):
        if name == "codebase_hotspots":
            return self._load("hotspots.json")
        if name == "adr_read":
            return self._load("adrs.json")
        if name == "issue_create":
            return {"issue_url": "https://github.com/example/fixture/issues/1"}
        if name == "codebase_search":
            query = params.get("query", "").lower()
            if "directory structure" in query:
                return self._load("recon.json")
            if "entry point" in query:
                return self._load("files.json")
            return self._load("interfaces.json")
        return {}


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------

def _findings(output: dict) -> list[dict]:
    return output.get("findings", []) or []


def _high_findings(output: dict) -> list[dict]:
    return [f for f in _findings(output) if _SEVERITY_RANK.get(f.get("severity", ""), 0) >= 3]


def _finding_text(f: dict) -> str:
    return " ".join(str(f.get(k, "")) for k in ("title", "message", "location", "category")).lower()


def _pattern_in_high(pattern: str, min_severity: str, output: dict) -> bool:
    floor = _SEVERITY_RANK.get(min_severity, 3)
    for f in _findings(output):
        if _SEVERITY_RANK.get(f.get("severity", ""), 0) < floor:
            continue
        if re.search(pattern, _finding_text(f), re.IGNORECASE):
            return True
    return False


def _schema_valid(output: dict) -> bool:
    try:
        jsonschema.validate(output, ARCHITECT_OUTPUT_SCHEMA)
        return True
    except jsonschema.ValidationError:
        return False


def _score(label: dict, output: dict) -> dict:
    must_flag = label.get("must_flag", [])
    flagged = [m for m in must_flag if _pattern_in_high(m["pattern"], m.get("min_severity", "HIGH"), output)]
    recall = len(flagged) / len(must_flag) if must_flag else 1.0

    expect_high = label.get("expect_high_severity", False)
    highs = _high_findings(output)
    criticals = [f for f in _findings(output) if f.get("severity") == "CRITICAL"]
    if expect_high:
        detected = len(highs) >= 1
    else:
        detected = len(criticals) == 0  # control: must not raise a false CRITICAL

    return {
        "schema_valid": _schema_valid(output),
        "detected": detected,
        "expect_high": expect_high,
        "recall": recall,
        "flagged": len(flagged),
        "must_flag_count": len(must_flag),
        "high_count": len(highs),
        "critical_count": len(criticals),
    }


def _make_agent(case_dir: Path) -> ArchitectAgent:
    llm = OllamaProvider(
        host=os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
        model=os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        num_ctx=int(os.environ.get("OLLAMA_NUM_CTX", "8192")),
    )
    return ArchitectAgent(gateway=_MockGateway(case_dir), llm_provider=llm)


async def _run_case(label_path: Path) -> tuple[dict, dict]:
    label = json.loads(label_path.read_text())
    case_dir = FIXTURES_DIR / label_path.stem
    agent = _make_agent(case_dir)
    state = {"task": f"Architecture review of {label_path.stem}", "thread_id": "eval"}
    result = await agent.run(state)
    return label, result


# ---------------------------------------------------------------------------
# Parametrized per-fixture test
# ---------------------------------------------------------------------------

def _load_cases():
    cases = []
    for label_path in sorted(LABELS_DIR.glob("*.json")):
        if (FIXTURES_DIR / label_path.stem).is_dir():
            cases.append(pytest.param(label_path, id=label_path.stem))
    return cases


@pytest.mark.eval
@pytest.mark.parametrize("label_path", _load_cases())
async def test_architect_fixture(label_path: Path):
    label, result = await _run_case(label_path)

    assert result.get("error") is None, f"Agent returned error: {result.get('error')}"
    output = result["agent_output"]
    score = _score(label, output)

    print(f"\n  [{label_path.stem}]")
    print(f"  schema_valid: {score['schema_valid']}")
    print(f"  detected:     {score['detected']} (expect_high={score['expect_high']})")
    print(f"  recall:       {score['flagged']}/{score['must_flag_count']} must-flag patterns in HIGH+ findings")
    print(f"  findings:     {score['high_count']} HIGH+, {score['critical_count']} CRITICAL")
    print(f"  summary:      {output.get('summary', '')[:120]}")

    assert score["schema_valid"], (
        f"Synthesis output failed ARCHITECT_OUTPUT_SCHEMA\n{json.dumps(output, indent=2)[:1500]}"
    )
    if score["expect_high"]:
        assert score["detected"], (
            "Expected at least one HIGH/CRITICAL finding for a planted smell\n"
            f"Findings: {json.dumps(_findings(output), indent=2)}"
        )
        if label.get("must_flag"):
            assert score["recall"] >= 0.5, (
                f"Recall too low ({score['recall']:.0%}): missed must-flag patterns\n"
                f"HIGH+ findings: {json.dumps(_high_findings(output), indent=2)}"
            )
    else:
        assert score["detected"], (
            f"Control fixture produced {score['critical_count']} false CRITICAL finding(s)\n"
            f"Findings: {json.dumps(_findings(output), indent=2)}"
        )


# ---------------------------------------------------------------------------
# Aggregate score report across all fixtures
# ---------------------------------------------------------------------------

def _compute_aggregates(scores: list[dict]) -> tuple[float, float, float]:
    schema_validity = sum(s["schema_valid"] for s in scores) / len(scores)
    detection_accuracy = sum(s["detected"] for s in scores) / len(scores)
    recall_scores = [s["recall"] for s in scores if s["must_flag_count"] > 0]
    avg_recall = sum(recall_scores) / len(recall_scores) if recall_scores else 1.0
    return schema_validity, detection_accuracy, avg_recall


@pytest.mark.eval
async def test_architect_aggregate_score():
    """Run all fixtures and assert minimum schema validity, detection and recall."""
    scores = []
    for label_path in sorted(LABELS_DIR.glob("*.json")):
        if not (FIXTURES_DIR / label_path.stem).is_dir():
            continue
        label, result = await _run_case(label_path)
        if result.get("error"):
            continue
        scores.append(_score(label, result["agent_output"]))

    assert scores
    schema_validity, detection_accuracy, avg_recall = _compute_aggregates(scores)

    print(f"\n  Fixtures scored:    {len(scores)}")
    print(f"  Schema validity:    {schema_validity:.0%}  (pass bar: 100%)")
    print(f"  Detection accuracy: {detection_accuracy:.0%}  (pass bar: 66%)")
    print(f"  Avg recall:         {avg_recall:.0%}  (pass bar: 50%)")

    assert schema_validity == 1.0
    assert detection_accuracy >= 0.66
    assert avg_recall >= 0.50
