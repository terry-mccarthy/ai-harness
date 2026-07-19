"""Schema unit tests for ADVERSARIAL_CODE_CRITIC_SCHEMA — no LLM required.

A confirmed/escalated CRITICAL finding must carry a concrete exploit_scenario
("forced artifact" instead of a bare severity label). Every other outcome/
severity combination does not require one.
"""
import jsonschema
import pytest

from harness_agents.types import ADVERSARIAL_CODE_CRITIC_SCHEMA


def _finding(**overrides) -> dict:
    base = {
        "outcome": "confirmed",
        "severity": "CRITICAL",
        "file": "app/db.py",
        "line": 12,
        "message": "SQL built via string concatenation",
        "exploit_scenario": "username=\"'; DROP TABLE users; --\" returns all rows and drops the table",
    }
    base.update(overrides)
    return base


def _payload(*findings, summary: str = "critic pass complete") -> dict:
    return {"findings": list(findings), "summary": summary}


def test_confirmed_critical_with_exploit_scenario_is_valid():
    jsonschema.validate(_payload(_finding()), ADVERSARIAL_CODE_CRITIC_SCHEMA)


def test_confirmed_critical_missing_exploit_scenario_is_invalid():
    finding = _finding()
    del finding["exploit_scenario"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_payload(finding), ADVERSARIAL_CODE_CRITIC_SCHEMA)


def test_escalated_critical_missing_exploit_scenario_is_invalid():
    finding = _finding(outcome="escalated")
    del finding["exploit_scenario"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_payload(finding), ADVERSARIAL_CODE_CRITIC_SCHEMA)


def test_confirmed_critical_with_empty_exploit_scenario_is_invalid():
    finding = _finding(exploit_scenario="")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_payload(finding), ADVERSARIAL_CODE_CRITIC_SCHEMA)


def test_refuted_critical_does_not_require_exploit_scenario():
    finding = _finding(outcome="refuted")
    del finding["exploit_scenario"]
    jsonschema.validate(_payload(finding), ADVERSARIAL_CODE_CRITIC_SCHEMA)


def test_downgraded_warning_does_not_require_exploit_scenario():
    finding = _finding(outcome="downgraded", severity="WARNING")
    del finding["exploit_scenario"]
    jsonschema.validate(_payload(finding), ADVERSARIAL_CODE_CRITIC_SCHEMA)


def test_confirmed_warning_does_not_require_exploit_scenario():
    """The forced-artifact rule only binds at CRITICAL severity."""
    finding = _finding(outcome="confirmed", severity="WARNING")
    del finding["exploit_scenario"]
    jsonschema.validate(_payload(finding), ADVERSARIAL_CODE_CRITIC_SCHEMA)


def test_unresolved_outcome_is_valid():
    finding = _finding(outcome="unresolved")
    del finding["exploit_scenario"]
    jsonschema.validate(_payload(finding), ADVERSARIAL_CODE_CRITIC_SCHEMA)


def test_invalid_outcome_enum_value_is_rejected():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_payload(_finding(outcome="maybe")), ADVERSARIAL_CODE_CRITIC_SCHEMA)


def test_missing_required_top_level_summary_is_invalid():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"findings": [_finding()]}, ADVERSARIAL_CODE_CRITIC_SCHEMA)
