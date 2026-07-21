"""OPA policy tests for the adversarial_code_critic agent role.

Verifies harness.rego's `allow` rule: the critic can call its two read-only
tools (git_diff, run_linter) and is denied everything else, following the
same least-privilege model as every other agent role.

All tests are @pytest.mark.integration (require a live OPA instance).
"""
import os
import pytest
import httpx

OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")


def _allow(tool_name: str) -> bool:
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/allow",
        json={"input": {"agent_role": "adversarial_code_critic", "tool_name": tool_name}},
        timeout=5.0,
    )
    assert resp.status_code == 200
    return bool(resp.json().get("result"))


@pytest.mark.integration
def test_opa_allows_adversarial_code_critic_git_diff():
    assert _allow("git_diff") is True


@pytest.mark.integration
def test_opa_allows_adversarial_code_critic_run_linter():
    assert _allow("run_linter") is True


@pytest.mark.integration
def test_opa_denies_adversarial_code_critic_shell_exec():
    assert _allow("shell_exec") is False


@pytest.mark.integration
def test_opa_denies_adversarial_code_critic_issue_create():
    assert _allow("issue_create") is False


@pytest.mark.integration
def test_opa_denies_adversarial_code_critic_codebase_search():
    """The critic gets no architect tools — its surface is diff/linter only."""
    assert _allow("codebase_search") is False
