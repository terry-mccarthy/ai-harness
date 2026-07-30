"""Fail-fast tripwire for the HS256 human-approval-token secret (issue #02).

`harness_supervisor.graph` must refuse to import if JWT_SECRET resolves to the
well-known dev-default and ENV != "test" — mirrors the existing RS256 agent-auth
tripwire in services/governance/core/config.py (ADR 0024).

These tests mutate process env and reload `harness_supervisor.graph` in-place,
so each test restores env + reloads the module back to a healthy state before
returning control to the rest of the suite (other test modules import this
module too).
"""
import importlib

import pytest


def _reload_graph():
    import harness_supervisor.graph as graph_module
    return importlib.reload(graph_module)


def test_tripwire_fires_for_dev_default_secret_outside_env_test(monkeypatch):
    """Dev-default JWT_SECRET (or unset) + ENV != 'test' must raise at import time."""
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("ENV", "production")
    try:
        with pytest.raises(RuntimeError, match="JWT_SECRET"):
            _reload_graph()
    finally:
        # Revert env immediately (not at fixture teardown) so the restoring
        # reload below runs against a healthy environment.
        monkeypatch.undo()
        _reload_graph()


def test_tripwire_allows_dev_default_secret_under_env_test(monkeypatch):
    """ENV=test (as the test suite sets by default) must not be blocked."""
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setenv("ENV", "test")
    try:
        module = _reload_graph()
        assert module._JWT_SECRET == "dev-jwt-secret-change-in-prod-xyz"
    finally:
        monkeypatch.undo()
        _reload_graph()


def test_tripwire_allows_real_secret_in_any_env(monkeypatch):
    """A real JWT_SECRET must work regardless of ENV."""
    monkeypatch.setenv("JWT_SECRET", "a-real-production-secret-value")
    monkeypatch.setenv("ENV", "production")
    try:
        module = _reload_graph()
        assert module._JWT_SECRET == "a-real-production-secret-value"
    finally:
        monkeypatch.undo()
        _reload_graph()
