"""Skill execution and revocation — issue 06.

Tests:
- GET /skills/{id} returns skill or 410 for revoked
- POST /skills/{id}/revoke sets status revoked; agent tokens 403
- execute_skill runs all steps, returns structured results
- on_failure=ABORT stops execution on step denial
- on_failure=CONTINUE skips denied step and carries on
- on_failure=ROLLBACK runs rollback steps before raising
- execute_skill on revoked skill raises immediately (no tool calls)
"""

import json
import os
import uuid

import httpx
import pymysql
import pymysql.cursors
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
MCPJUNGLE_URL = os.environ.get("MCPJUNGLE_URL", "http://localhost:8080")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))


def _get_token(client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _human_token() -> str:
    return _get_token("human-operator", os.environ.get("HUMAN_OPERATOR_SECRET", "human-operator-secret"))


def _root_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root",
        database="harness", connect_timeout=5, autocommit=True,
    )


def _insert_skill(steps: list, agent_role: str = "sre", status: str = "active") -> str:
    skill_id = f"test:{uuid.uuid4().hex[:10]}"
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skills "
                "(id, name, agent_role, version, status, input_schema, steps, output_contract, promoted_by, created_at) "
                "VALUES (%s,%s,%s,1,%s,'{}', %s,'{}','test-runner',NOW())",
                (skill_id, skill_id, agent_role, status, json.dumps(steps)),
            )
        conn.commit()
    return skill_id


def _sre_client():
    from harness_gateway.client import GatewayClient
    return GatewayClient(
        gateway_url=MCPJUNGLE_URL,
        governance_url=GOVERNANCE_URL,
        client_id="sre",
        client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
    )


# ---------------------------------------------------------------------------
# GET /skills/{id}
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_skill_returns_200():
    """GET /skills/{id} returns the skill record for an active skill."""
    token = _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills/sre:triage-incident",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == "sre:triage-incident"


@pytest.mark.integration
def test_get_revoked_skill_returns_410():
    """GET /skills/{id} returns 410 for a revoked skill."""
    skill_id = _insert_skill([{"action": "log_search"}], status="revoked")
    token = _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills/{skill_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 410, resp.text


@pytest.mark.integration
def test_get_missing_skill_returns_404():
    token = _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills/does-not-exist",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# POST /skills/{id}/revoke
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_revoke_sets_status_revoked():
    """POST /skills/{id}/revoke transitions skill to revoked."""
    skill_id = _insert_skill([{"action": "log_search"}])
    token = _human_token()
    resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/{skill_id}/revoke",
        json={"reason": "procedure outdated"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.text

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT status, revoked_reason FROM skills WHERE id=%s ORDER BY version DESC LIMIT 1", (skill_id,))
            row = cur.fetchone()
    assert row["status"] == "revoked"
    assert row["revoked_reason"] == "procedure outdated"


@pytest.mark.integration
def test_agent_cannot_revoke():
    """sre role lacks skill:promote scope → 403 on revoke."""
    skill_id = _insert_skill([{"action": "log_search"}])
    token = _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))
    resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/{skill_id}/revoke",
        json={"reason": "test"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.integration
def test_revoke_without_reason_returns_422():
    skill_id = _insert_skill([{"action": "log_search"}])
    resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/{skill_id}/revoke",
        json={},
        headers={"Authorization": f"Bearer {_human_token()}"},
        timeout=10.0,
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# execute_skill — happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_skill_runs_all_steps():
    """execute_skill with two allowed steps returns both results."""
    skill_id = _insert_skill([
        {"action": "observability_query", "on_failure": "ABORT"},
        {"action": "log_search", "on_failure": "ABORT"},
    ])
    client = _sre_client()
    result = await client.execute_skill(skill_id, {"query": "test"})
    assert result["steps_completed"] == 2
    assert len(result["results"]) == 2


# ---------------------------------------------------------------------------
# execute_skill — on_failure modes
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_abort_on_step_denial():
    """ABORT on_failure stops execution when a step is denied."""
    from harness_gateway.client import ToolAccessDenied
    skill_id = _insert_skill([
        {"action": "log_search", "on_failure": "ABORT"},
        # codebase_search requires architect role — sre gets denied
        {"action": "codebase_search", "on_failure": "ABORT"},
        {"action": "observability_query", "on_failure": "ABORT"},  # never reached
    ])
    client = _sre_client()
    with pytest.raises(ToolAccessDenied):
        await client.execute_skill(skill_id, {})
    # Only step 1 (log_search) completed; step 2 denied before HTTP call; step 3 never reached
    assert len(client.last_calls) == 1
    assert client.last_calls[0]["tool"] == "log_search"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_continue_on_step_denial():
    """CONTINUE skips denied step and runs the next one."""
    skill_id = _insert_skill([
        {"action": "log_search", "on_failure": "CONTINUE"},
        {"action": "codebase_search", "on_failure": "CONTINUE"},  # denied, skipped
        {"action": "observability_query", "on_failure": "CONTINUE"},
    ])
    client = _sre_client()
    result = await client.execute_skill(skill_id, {})
    # 3 entries: 2 successful + 1 skipped
    assert len(result["results"]) == 3
    skipped = [r for r in result["results"] if r.get("skipped")]
    assert len(skipped) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rollback_runs_rollback_steps_then_raises():
    """ROLLBACK executes rollback steps before raising."""
    from harness_gateway.client import ToolAccessDenied
    skill_id = _insert_skill([
        {"action": "codebase_search", "on_failure": "ROLLBACK",
         "rollback_steps": [{"action": "log_search"}]},  # rollback is allowed for sre
    ])
    client = _sre_client()
    with pytest.raises(ToolAccessDenied):
        await client.execute_skill(skill_id, {})
    # rollback tool was called
    rollback_calls = [c for c in client.last_calls if c["tool"] == "log_search"]
    assert len(rollback_calls) >= 1


# ---------------------------------------------------------------------------
# execute_skill on revoked skill
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_revoked_skill_raises():
    """Executing a revoked skill raises ToolAccessDenied immediately."""
    from harness_gateway.client import ToolAccessDenied
    skill_id = _insert_skill([{"action": "log_search"}], status="revoked")
    client = _sre_client()
    with pytest.raises(ToolAccessDenied):
        await client.execute_skill(skill_id, {})
    # No tool calls made
    tool_calls = [c for c in client.last_calls if c["tool"] == "log_search"]
    assert len(tool_calls) == 0
