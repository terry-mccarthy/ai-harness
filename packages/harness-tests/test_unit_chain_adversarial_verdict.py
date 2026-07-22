"""Unit tests for the shared chained-verdict helper used by both the code and
architecture adversarial-review chaining (issues #03/#04). Pure function, no
LLM/gateway dependency.
"""
import importlib.util
import sys
from pathlib import Path

_REVIEW_SERVER_PATH = Path(__file__).resolve().parents[2] / "services" / "review_server" / "server.py"
_REVIEW_SERVER_MODULE = "_review_server_under_test"


def _load_review_server():
    if _REVIEW_SERVER_MODULE in sys.modules:
        return sys.modules[_REVIEW_SERVER_MODULE]
    rs_dir = str(_REVIEW_SERVER_PATH.parent)
    if rs_dir not in sys.path:
        sys.path.insert(0, rs_dir)
    spec = importlib.util.spec_from_file_location(_REVIEW_SERVER_MODULE, _REVIEW_SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_REVIEW_SERVER_MODULE] = mod
    spec.loader.exec_module(mod)
    return mod


def _finding(severity, outcome):
    return {"outcome": outcome, "severity": severity, "message": "x"}


def test_no_findings_passes():
    server = _load_review_server()
    assert server._chain_adversarial_verdict([], {"CRITICAL"}) == "pass"


def test_confirmed_fail_severity_fails():
    server = _load_review_server()
    findings = [_finding("CRITICAL", "confirmed")]
    assert server._chain_adversarial_verdict(findings, {"CRITICAL"}) == "fail"


def test_escalated_fail_severity_fails():
    server = _load_review_server()
    findings = [_finding("CRITICAL", "escalated")]
    assert server._chain_adversarial_verdict(findings, {"CRITICAL"}) == "fail"


def test_refuted_fail_severity_passes():
    server = _load_review_server()
    findings = [_finding("CRITICAL", "refuted")]
    assert server._chain_adversarial_verdict(findings, {"CRITICAL"}) == "pass"


def test_downgraded_fail_severity_passes():
    server = _load_review_server()
    findings = [_finding("CRITICAL", "downgraded")]
    assert server._chain_adversarial_verdict(findings, {"CRITICAL"}) == "pass"


def test_unresolved_fail_severity_passes():
    server = _load_review_server()
    findings = [_finding("CRITICAL", "unresolved")]
    assert server._chain_adversarial_verdict(findings, {"CRITICAL"}) == "pass"


def test_confirmed_below_fail_severity_passes():
    server = _load_review_server()
    findings = [_finding("WARNING", "confirmed")]
    assert server._chain_adversarial_verdict(findings, {"CRITICAL"}) == "pass"


def test_architecture_high_plus_threshold():
    server = _load_review_server()
    findings = [_finding("HIGH", "confirmed")]
    assert server._chain_adversarial_verdict(findings, {"CRITICAL", "HIGH"}) == "fail"


def test_architecture_medium_does_not_fail_high_plus_threshold():
    server = _load_review_server()
    findings = [_finding("MEDIUM", "confirmed")]
    assert server._chain_adversarial_verdict(findings, {"CRITICAL", "HIGH"}) == "pass"


def test_one_confirmed_fail_among_many_passing_still_fails():
    server = _load_review_server()
    findings = [
        _finding("WARNING", "confirmed"),
        _finding("CRITICAL", "refuted"),
        _finding("CRITICAL", "confirmed"),
    ]
    assert server._chain_adversarial_verdict(findings, {"CRITICAL"}) == "fail"
