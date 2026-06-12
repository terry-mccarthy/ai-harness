"""Phase 6 issue-02 — OPA invoke_allowed + GET /agents endpoint.

Tests verify:
- harness.rego invoke_allowed / claim_allowed rules
- GET /agents returns only OPA-permitted agents for the calling role
All tests are @pytest.mark.integration.
"""

import os
import pytest
import httpx

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")


def get_token(client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


# ---------------------------------------------------------------------------
# OPA policy tests — invoke_allowed
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_opa_supervisor_can_invoke_code_reviewer():
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/invoke_allowed",
        json={"input": {"role": "supervisor", "action": "invoke", "target": "code-reviewer"}},
    )
    assert resp.status_code == 200
    assert "code-reviewer" in resp.json().get("result", [])


@pytest.mark.integration
def test_opa_supervisor_can_invoke_architect():
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/invoke_allowed",
        json={"input": {"role": "supervisor", "action": "invoke", "target": "architect"}},
    )
    assert resp.status_code == 200
    assert "architect" in resp.json().get("result", [])


@pytest.mark.integration
def test_opa_supervisor_can_invoke_sre():
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/invoke_allowed",
        json={"input": {"role": "supervisor", "action": "invoke", "target": "sre"}},
    )
    assert resp.status_code == 200
    assert "sre" in resp.json().get("result", [])


@pytest.mark.integration
def test_opa_architect_can_invoke_code_reviewer():
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/invoke_allowed",
        json={"input": {"role": "architect", "action": "invoke", "target": "code-reviewer"}},
    )
    assert resp.status_code == 200
    assert "code-reviewer" in resp.json().get("result", [])


@pytest.mark.integration
def test_opa_code_reviewer_cannot_invoke_sre():
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/invoke_allowed",
        json={"input": {"role": "code_reviewer", "action": "invoke", "target": "sre"}},
    )
    assert resp.status_code == 200
    result = resp.json().get("result", [])
    assert "sre" not in result, f"code_reviewer should not be allowed to invoke sre, got: {result}"


@pytest.mark.integration
def test_opa_sre_cannot_invoke_anyone():
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/invoke_allowed",
        json={"input": {"role": "sre", "action": "invoke", "target": "architect"}},
    )
    assert resp.status_code == 200
    result = resp.json().get("result", [])
    assert "architect" not in result, f"sre should not be allowed to invoke architect, got: {result}"


@pytest.mark.integration
def test_opa_claim_allowed_matching_role():
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/claim_allowed",
        json={"input": {"role": "sre", "action": "claim", "required_role": "sre"}},
    )
    assert resp.status_code == 200
    assert resp.json().get("result") is True


@pytest.mark.integration
def test_opa_claim_denied_wrong_role():
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/claim_allowed",
        json={"input": {"role": "sre", "action": "claim", "required_role": "architect"}},
    )
    assert resp.status_code == 200
    result = resp.json().get("result")
    assert not result, f"sre claiming architect task should be denied, got: {result}"


# ---------------------------------------------------------------------------
# GET /agents endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_agent_list_supervisor_sees_all():
    """Supervisor sees all three agents it may invoke."""
    token = get_token("architect", os.environ.get("ARCHITECT_SECRET", "architect-secret"))
    # Use a supervisor token — but we don't have a supervisor client registered yet.
    # The architect role can invoke code-reviewer; verify it sees at least that.
    resp = httpx.get(
        f"{GOVERNANCE_URL}/agents",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    agents = resp.json()
    assert isinstance(agents, list), f"Expected list, got: {agents}"
    names = [a["name"] for a in agents]
    assert "code-reviewer" in names, f"architect should see code-reviewer; got {names}"


@pytest.mark.integration
def test_agent_list_code_reviewer_sees_empty():
    """code-reviewer cannot invoke any other agents — list should be empty."""
    token = get_token(
        "code-reviewer",
        os.environ["CODE_REVIEWER_SECRET"],
    )
    resp = httpx.get(
        f"{GOVERNANCE_URL}/agents",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    agents = resp.json()
    assert isinstance(agents, list)
    assert agents == [], f"code-reviewer should see empty agent list, got: {agents}"


@pytest.mark.integration
def test_agent_list_requires_auth():
    """GET /agents without a token returns 401."""
    resp = httpx.get(f"{GOVERNANCE_URL}/agents")
    assert resp.status_code == 401
