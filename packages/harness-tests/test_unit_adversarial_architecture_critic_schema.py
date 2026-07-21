"""Schema unit tests for ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA — no LLM required.

A confirmed/escalated HIGH+ finding must carry a concrete regression_scenario
("forced artifact" instead of a bare severity label). Every other outcome/
severity combination does not require one.
"""
import jsonschema
import pytest

from harness_agents.types import ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA


def _finding(**overrides) -> dict:
    base = {
        "outcome": "confirmed",
        "severity": "HIGH",
        "location": "shopflow/routes.py",
        "message": "Business logic lives inline in the HTTP handler, violating ADR-0001.",
        "regression_scenario": "A new payment provider requires touching every route handler instead of one service class, and the untested inline charge call already shipped a double-charge bug in prod.",
    }
    base.update(overrides)
    return base


def _payload(*findings, summary: str = "critic pass complete") -> dict:
    return {"findings": list(findings), "summary": summary}


def test_confirmed_high_with_regression_scenario_is_valid():
    jsonschema.validate(_payload(_finding()), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_confirmed_critical_with_regression_scenario_is_valid():
    jsonschema.validate(_payload(_finding(severity="CRITICAL")), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_confirmed_high_missing_regression_scenario_is_invalid():
    finding = _finding()
    del finding["regression_scenario"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_payload(finding), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_escalated_critical_missing_regression_scenario_is_invalid():
    finding = _finding(outcome="escalated", severity="CRITICAL")
    del finding["regression_scenario"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_payload(finding), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_confirmed_high_with_empty_regression_scenario_is_invalid():
    finding = _finding(regression_scenario="")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_payload(finding), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_refuted_high_does_not_require_regression_scenario():
    finding = _finding(outcome="refuted")
    del finding["regression_scenario"]
    jsonschema.validate(_payload(finding), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_downgraded_medium_does_not_require_regression_scenario():
    finding = _finding(outcome="downgraded", severity="MEDIUM")
    del finding["regression_scenario"]
    jsonschema.validate(_payload(finding), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_confirmed_medium_does_not_require_regression_scenario():
    """The forced-artifact rule only binds at HIGH+ severity."""
    finding = _finding(outcome="confirmed", severity="MEDIUM")
    del finding["regression_scenario"]
    jsonschema.validate(_payload(finding), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_unresolved_outcome_is_valid():
    finding = _finding(outcome="unresolved")
    del finding["regression_scenario"]
    jsonschema.validate(_payload(finding), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_invalid_outcome_enum_value_is_rejected():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_payload(_finding(outcome="maybe")), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_invalid_severity_enum_value_is_rejected():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_payload(_finding(severity="URGENT")), ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)


def test_missing_required_top_level_summary_is_invalid():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"findings": [_finding()]}, ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA)
