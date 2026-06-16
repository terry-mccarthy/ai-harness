"""Skill expiry and re-validation trigger — issue 07.

Tests:
- POST /skills/expire transitions overdue ACTIVE skills to EXPIRED
- Expired skills return 410 from GET /skills/{id}
- Expired skills are not executable (execute_skill raises)
- Response includes skill_ids and re_proposed_candidates
- Re-validation auto-proposes a candidate when N_MIN resolved episodes exist
- Re-validation does not fire when too few episodes exist
- Auto-trigger fires after EXPIRY_PASS_INTERVAL audit events
- Early-review flag set when trailing 30-day success rate < 0.5
- Early-review flag absent when success rate >= 0.5
"""

import json
import os
import time
import uuid

import httpx
import pymysql
import pymysql.cursors
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
MCPJUNGLE_URL = os.environ.get("MCPJUNGLE_URL", "http://localhost:8080")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))
EXPIRY_PASS_INTERVAL = int(os.environ.get("EXPIRY_PASS_INTERVAL", "3"))


def _human_token() -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "human-operator",
            "client_secret": os.environ.get("HUMAN_OPERATOR_SECRET", "human-operator-secret"),
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _sre_token() -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "sre",
            "client_secret": os.environ.get("SRE_SECRET", "sre-secret"),
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _root_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user="root",
        password="root",
        database="harness",
        connect_timeout=5,
        autocommit=True,
    )


def _insert_skill_with_expiry(expires_days_offset: int, agent_role: str = "sre") -> str:
    """Insert a skill with expires_at = NOW() + offset days. Negative = already expired."""
    skill_id = f"test:{uuid.uuid4().hex[:10]}"
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skills "
                "(id, name, agent_role, version, status, input_schema, steps, output_contract, promoted_by, "
                "expires_at, created_at) "
                "VALUES (%s, %s, %s, 1, 'active', '{}', %s, '{}', 'test-runner', "
                "DATE_ADD(NOW(), INTERVAL %s DAY), NOW())",
                (
                    skill_id, skill_id, agent_role,
                    json.dumps([{"action": "log_search"}]),
                    expires_days_offset,
                ),
            )
        conn.commit()
    return skill_id


def _insert_episode(agent_principal: str, outcome: str = "RESOLVED") -> str:
    episode_id = str(uuid.uuid4())
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes "
                "(episode_id, agent_principal, alert_signature, service_class, env_fingerprint, actions, outcome, outcome_labeled_at) "
                "VALUES (%s, %s, %s, 'test', '{}', '[]', %s, NOW())",
                (episode_id, agent_principal, f"{agent_principal}.log_search:{episode_id[:8]}", outcome),
            )
        conn.commit()
    return episode_id


def _insert_audit_log_row(agent_id: str, decision: str) -> None:
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_log "
                "(agent_id, tool_name, policy_decision, timestamp_ms) "
                "VALUES (%s, 'sre_stub__log_search', %s, %s)",
                (agent_id, decision, int(time.time() * 1000)),
            )
        conn.commit()


def _get_skill_status(skill_id: str) -> str | None:
    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT status FROM skills WHERE id=%s ORDER BY version DESC LIMIT 1",
                (skill_id,),
            )
            row = cur.fetchone()
    return row["status"] if row else None


def _call_expire(token: str | None = None) -> httpx.Response:
    tok = token or _human_token()
    return httpx.post(
        f"{GOVERNANCE_URL}/skills/expire",
        headers={"Authorization": f"Bearer {tok}"},
        timeout=15.0,
    )


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_expire_requires_human_operator_role():
    """SRE role cannot call POST /skills/expire (403)."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/expire",
        headers={"Authorization": f"Bearer {_sre_token()}"},
        timeout=10.0,
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Basic expiry
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_expire_returns_200_with_no_overdue_skills():
    """When no skills are overdue, expire returns empty summary."""
    resp = _call_expire()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "expired_count" in data
    assert "skill_ids" in data
    assert "re_proposed_candidates" in data
    assert "flagged_for_early_review" in data


@pytest.mark.integration
def test_expire_transitions_overdue_skill_to_expired():
    """POST /skills/expire sets status=expired for overdue skill."""
    skill_id = _insert_skill_with_expiry(-1)  # expired yesterday
    resp = _call_expire()
    assert resp.status_code == 200, resp.text
    assert _get_skill_status(skill_id) == "expired"


@pytest.mark.integration
def test_expire_response_includes_skill_id():
    """POST /skills/expire response includes the expired skill id."""
    skill_id = _insert_skill_with_expiry(-1)
    resp = _call_expire()
    assert skill_id in resp.json()["skill_ids"]


@pytest.mark.integration
def test_expire_does_not_touch_non_overdue_skills():
    """POST /skills/expire leaves future-expiring skills alone."""
    future_skill = _insert_skill_with_expiry(30)
    _call_expire()
    assert _get_skill_status(future_skill) == "active"


# ---------------------------------------------------------------------------
# Expired skill becomes unreachable
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_get_expired_skill_returns_410():
    """GET /skills/{id} returns 410 for an expired skill."""
    skill_id = _insert_skill_with_expiry(-1)
    _call_expire()
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills/{skill_id}",
        headers={"Authorization": f"Bearer {_sre_token()}"},
        timeout=10.0,
    )
    assert resp.status_code == 410, resp.text


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_expired_skill_raises():
    """execute_skill on an expired skill raises ToolAccessDenied immediately."""
    from harness_gateway.client import GatewayClient, ToolAccessDenied

    skill_id = _insert_skill_with_expiry(-1)
    _call_expire()

    client = GatewayClient(
        gateway_url=MCPJUNGLE_URL,
        governance_url=GOVERNANCE_URL,
        client_id="sre",
        client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
    )
    with pytest.raises(ToolAccessDenied):
        await client.execute_skill(skill_id, {})


# ---------------------------------------------------------------------------
# Re-validation candidate auto-proposal
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_revalidation_proposes_candidate_when_enough_episodes():
    """Auto-propose candidate for expired skill when N_MIN resolved episodes exist."""
    skill_id = _insert_skill_with_expiry(-1, agent_role="sre")

    # Insert N_MIN=5 resolved episodes from the same agent
    for _ in range(5):
        _insert_episode("sre", "RESOLVED")

    resp = _call_expire()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert len(data["re_proposed_candidates"]) >= 1


@pytest.mark.integration
def test_revalidation_not_triggered_when_too_few_episodes():
    """Auto-proposal is skipped when fewer than N_MIN resolved episodes exist."""
    skill_id = _insert_skill_with_expiry(-1, agent_role=f"test-sre-{uuid.uuid4().hex[:6]}")
    # Insert only 2 episodes — below N_MIN=5
    for _ in range(2):
        _insert_episode(f"test-sre-{uuid.uuid4().hex[:6]}", "RESOLVED")

    resp = _call_expire()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    # The skill should be expired but no candidate proposed
    assert skill_id in data["skill_ids"]
    # re_proposed might have entries from other tests; just check none link to our skill
    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT candidate_id FROM candidates WHERE cluster_key=%s AND status='PROPOSED'",
                (skill_id,),
            )
            rows = cur.fetchall()
    assert len(rows) == 0


# ---------------------------------------------------------------------------
# Auto-trigger via audit counter
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_auto_trigger_expires_skill_after_interval_audit_calls():
    """Posting EXPIRY_PASS_INTERVAL audit events triggers background expiry pass."""
    skill_id = _insert_skill_with_expiry(-1)
    token = _sre_token()
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(EXPIRY_PASS_INTERVAL):
        httpx.post(
            f"{GOVERNANCE_URL}/audit",
            json={"tool_name": "sre_stub__log_search", "decision": "allow", "latency_ms": 10},
            headers=headers,
            timeout=10.0,
        )

    # Give background task time to complete
    time.sleep(1.0)
    assert _get_skill_status(skill_id) == "expired"


# ---------------------------------------------------------------------------
# Early-review flag
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_early_review_flag_set_for_low_success_rate():
    """Skill with < 50% allowed audit calls in last 30 days is flagged."""
    unique_role = f"test-role-{uuid.uuid4().hex[:8]}"
    skill_id = _insert_skill_with_expiry(30, agent_role=unique_role)  # not yet expired

    # 1 allow + 5 deny = ~17% success rate
    _insert_audit_log_row(unique_role, "allow")
    for _ in range(5):
        _insert_audit_log_row(unique_role, "deny")

    resp = _call_expire()
    assert resp.status_code == 200, resp.text
    assert skill_id in resp.json()["flagged_for_early_review"]


@pytest.mark.integration
def test_early_review_flag_absent_for_high_success_rate():
    """Skill with >= 50% allowed audit calls is NOT flagged."""
    unique_role = f"test-role-{uuid.uuid4().hex[:8]}"
    skill_id = _insert_skill_with_expiry(30, agent_role=unique_role)

    # 5 allow + 1 deny = ~83% success rate
    for _ in range(5):
        _insert_audit_log_row(unique_role, "allow")
    _insert_audit_log_row(unique_role, "deny")

    resp = _call_expire()
    assert resp.status_code == 200, resp.text
    assert skill_id not in resp.json()["flagged_for_early_review"]
