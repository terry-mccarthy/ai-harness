"""Unit tests for the architecture gate runner and checkers.

Mocks subprocess to avoid requiring CLI tools in the test environment.
"""
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# architecture_gate lives under services/review_server/
_GATE_DIR = Path(__file__).resolve().parents[2] / "services" / "review_server"
sys.path.insert(0, str(_GATE_DIR))

from architecture_gate.models import GateSignal, Violation
from architecture_gate.runner import run_gate
from architecture_gate.registry import REGISTRY
from architecture_gate.checkers.import_linter import ImportLinterChecker
from architecture_gate.checkers.xenon_checker import XenonChecker


# ---------------------------------------------------------------------------
# Tracer bullet: unknown language returns PASS
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_language_returns_pass():
    """No checkers registered for the language → PASS with no violations."""
    signal = await run_gate("/tmp/test-repo", "unknown_lang_xyz")
    assert signal.result == "PASS"
    assert len(signal.violations) == 0


# ---------------------------------------------------------------------------
# ImportLinterChecker
# ---------------------------------------------------------------------------


def _mock_subprocess(returncode: int, stdout: str = "", stderr: str = ""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.mark.asyncio
async def test_import_linter_clean_repo():
    """No violations → empty list from ImportLinterChecker."""
    with patch("architecture_gate.checkers.import_linter.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess(0, stdout=json.dumps({"contracts": []}))
        checker = ImportLinterChecker()
        violations = await checker.check("/tmp/test-repo")

    assert len(violations) == 0


@pytest.mark.asyncio
async def test_import_linter_catches_layer_violation():
    """Layer violation → HARD Violation with correct rule and message."""
    fake_output = {
        "contracts": [
            {
                "name": "Layer enforcement",
                "violations": [
                    {
                        "rule": "layer-violation",
                        "module": "infrastructure/db.py",
                        "message": "Illegal import: infrastructure cannot import domain",
                    }
                ],
            }
        ],
    }
    with patch("architecture_gate.checkers.import_linter.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess(1, stdout=json.dumps(fake_output))
        checker = ImportLinterChecker()
        violations = await checker.check("/tmp/test-repo")

    assert len(violations) == 1
    assert violations[0].severity == "HARD"
    assert violations[0].rule == "layer-violation"
    assert "infrastructure/db.py" in violations[0].file


# ---------------------------------------------------------------------------
# XenonChecker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_xenon_clean_repo():
    """No complexity violations → empty list from XenonChecker."""
    with patch("architecture_gate.checkers.xenon_checker.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess(0)
        checker = XenonChecker()
        violations = await checker.check("/tmp/test-repo")
    assert len(violations) == 0


@pytest.mark.asyncio
async def test_xenon_catches_complexity_violation():
    """Block with rank F → SOFT Violation."""
    fake_stderr = (
        "src/core/processor.py:42:0: M: myproject.MyClass.my_method - F\n"
        "src/core/processor.py:10:0: F: myproject.helper - B\n"
    )
    with patch("architecture_gate.checkers.xenon_checker.subprocess.run") as mock_run:
        mock_run.return_value = _mock_subprocess(1, stderr=fake_stderr)
        checker = XenonChecker()
        violations = await checker.check("/tmp/test-repo")

    # Only rank F exceeds B, rank B does not
    assert len(violations) == 1
    assert violations[0].severity == "SOFT"
    assert violations[0].rule == "complexity-limit"
    assert "processor.py:42" in violations[0].file


# ---------------------------------------------------------------------------
# Runner — short-circuit on HARD
# ---------------------------------------------------------------------------


class _HardChecker:
    """Stub checker that returns a HARD violation."""

    async def check(self, repo_path: str) -> list:
        return [Violation(rule="layer-violation", severity="HARD", file="a.py", message="bad")]


class _SoftChecker:
    """Stub checker that returns a SOFT violation."""

    async def check(self, repo_path: str) -> list:
        return [Violation(rule="complexity-limit", severity="SOFT", file="b.py", message="complex")]


class _PassChecker:
    """Stub checker that returns no violations."""

    async def check(self, repo_path: str) -> list:
        return []


@pytest.mark.asyncio
async def test_runner_short_circuits_on_hard():
    """When a HARD violation is found, remaining checkers are not called and action is STOP_AND_SURFACE."""
    checker2 = _SoftChecker()
    checker2.called = False
    original_check = checker2.check

    async def track_call(repo_path):
        checker2.called = True
        return await original_check(repo_path)

    checker2.check = track_call

    # Patch REGISTRY so only these two checkers run
    with patch("architecture_gate.runner.REGISTRY", {"python": [_HardChecker(), checker2]}):
        signal = await run_gate("/tmp/test-repo", "python")

    assert signal.result == "FAIL"
    assert len(signal.violations) == 1
    assert signal.action == "STOP_AND_SURFACE"
    assert not checker2.called, "second checker should not have been called"


@pytest.mark.asyncio
async def test_runner_proceeds_on_soft_only():
    """SOFT violations without HARD → FAIL with PROCEED action."""
    with patch("architecture_gate.runner.REGISTRY", {"python": [_SoftChecker()]}):
        signal = await run_gate("/tmp/test-repo", "python")

    assert signal.result == "FAIL"
    assert len(signal.violations) == 1
    assert signal.action == "PROCEED"


# ---------------------------------------------------------------------------
# Error handling — tool not installed / timeout
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_linter_not_installed():
    """FileNotFoundError → gracefully returns empty list."""
    with patch("architecture_gate.checkers.import_linter.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError()
        checker = ImportLinterChecker()
        violations = await checker.check("/tmp/test-repo")
    assert len(violations) == 0


@pytest.mark.asyncio
async def test_import_linter_timeout():
    """TimeoutExpired → gracefully returns empty list."""
    with patch("architecture_gate.checkers.import_linter.subprocess.run") as mock_run:
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="import-linter", timeout=60)
        checker = ImportLinterChecker()
        violations = await checker.check("/tmp/test-repo")
    assert len(violations) == 0
