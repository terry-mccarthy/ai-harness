"""Phase 6 issue-06 — Supervisor demo: chained reviewer→architect workflow.

Tests verify:
- chained invocation: architect invokes code-reviewer, result is returned
- each agent call is audited under the correct agent_role
- schemas are validated at each handoff
- no token forwarding (each agent uses its own credentials)
All tests are @pytest.mark.integration.
"""

import os
import time
import pytest
import httpx
import pymysql

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))


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


def get_root_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root",
        database="harness", connect_timeout=5,
    )


@pytest.fixture
def architect_token():
    return get_token("architect", os.environ.get("ARCHITECT_SECRET", "architect-secret"))


@pytest.fixture
def reviewer_token():
    return get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])


# ---------------------------------------------------------------------------
# Chained workflow: architect → code-reviewer
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_supervisor_chain_reviewer_to_architect(architect_token):
    """Architect invokes code-reviewer; structured result returned.
    This is the first multi-agent chain in the harness.
    """
    before_ts = int(time.time() * 1000)

    # Step 1: architect invokes code-reviewer
    review_resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "code-reviewer",
            "artifact_type": "git_diff",
            "payload": {"repo": "test", "base_ref": "HEAD~1", "head_ref": "HEAD"},
        },
        headers={"Authorization": f"Bearer {architect_token}"},
        timeout=60.0,
    )
    assert review_resp.status_code == 200, review_resp.text
    review_result = review_resp.json()
    assert isinstance(review_result, dict), f"Expected dict, got: {type(review_result)}"

    # Step 2: verify audit trail shows code-reviewer as agent (not architect)
    time.sleep(0.5)
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT agent_id, tool_name FROM audit_log "
                "WHERE agent_id='code-reviewer' AND timestamp_ms >= %s "
                "ORDER BY timestamp_ms DESC LIMIT 5",
                (before_ts,),
            )
            rows = cur.fetchall()
    assert len(rows) > 0, (
        "Expected audit rows for code-reviewer agent; "
        "chained invocation should audit under target role, not caller"
    )


@pytest.mark.integration
def test_supervisor_schema_mismatch_raises_422(architect_token):
    """Payload that fails the target's input_schema raises 422 — chain fails loudly."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "code-reviewer",
            "artifact_type": "git_diff",
            "payload": {"wrong_field": "no_repo"},  # missing required 'repo'
        },
        headers={"Authorization": f"Bearer {architect_token}"},
    )
    assert resp.status_code == 422, (
        f"Schema mismatch should return 422, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.integration
def test_supervisor_no_token_forwarding(architect_token):
    """architect token is NOT forwarded to code-reviewer — target uses its own creds."""
    before_ts = int(time.time() * 1000)

    httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "code-reviewer",
            "artifact_type": "git_diff",
            "payload": {"repo": "test", "base_ref": "HEAD~1", "head_ref": "HEAD"},
        },
        headers={"Authorization": f"Bearer {architect_token}"},
        timeout=60.0,
    )

    time.sleep(0.5)
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            # There should be NO audit row where architect called review tools
            cur.execute(
                "SELECT agent_id, tool_name FROM audit_log "
                "WHERE agent_id='architect' AND timestamp_ms >= %s "
                "AND tool_name LIKE %s",
                (before_ts, "%%review%%"),
            )
            architect_review_rows = cur.fetchall()
    assert len(architect_review_rows) == 0, (
        f"architect token was forwarded to review tool — credential separation violated: "
        f"{architect_review_rows}"
    )


@pytest.mark.integration
def test_reviewer_cannot_chain_to_sre(reviewer_token):
    """Reviewer has no permission to invoke sre — topology policy enforced."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "sre",
            "artifact_type": "incident",
            "payload": {"alert": "injected-invoke"},
        },
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert resp.status_code == 403, (
        f"Expected 403 for reviewer→sre, got {resp.status_code}: {resp.text}"
    )
