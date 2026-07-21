"""OPA policy tests for the adversarial_architecture_critic agent role.

Verifies harness.rego's `allow` rule: the critic can call its three read-only
reconnaissance tools (codebase_search, adr_read, codebase_hotspots) and is
denied everything else — including issue_create, which stays scoped to the
first-pass architect — following the same least-privilege model as every
other agent role.

All tests are @pytest.mark.integration (require a live OPA instance).
"""
import os
import pytest
import httpx

OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")


def _allow(tool_name: str) -> bool:
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/allow",
        json={"input": {"agent_role": "adversarial_architecture_critic", "tool_name": tool_name}},
        timeout=5.0,
    )
    assert resp.status_code == 200
    return bool(resp.json().get("result"))


@pytest.mark.integration
def test_opa_allows_adversarial_architecture_critic_codebase_search():
    assert _allow("codebase_search") is True


@pytest.mark.integration
def test_opa_allows_adversarial_architecture_critic_adr_read():
    assert _allow("adr_read") is True


@pytest.mark.integration
def test_opa_allows_adversarial_architecture_critic_codebase_hotspots():
    assert _allow("codebase_hotspots") is True


@pytest.mark.integration
def test_opa_denies_adversarial_architecture_critic_issue_create():
    """Unlike the first-pass architect, the critic never files issues itself."""
    assert _allow("issue_create") is False


@pytest.mark.integration
def test_opa_denies_adversarial_architecture_critic_shell_exec():
    assert _allow("shell_exec") is False


@pytest.mark.integration
def test_opa_denies_adversarial_architecture_critic_git_diff():
    """The architecture critic gets no code-critic tools — its surface is recon only."""
    assert _allow("git_diff") is False
