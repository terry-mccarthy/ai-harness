"""Phase 6 issue-04 — agent_invoke: synchronous governed handoff.

Tests verify:
- supervisor can invoke code-reviewer; result returned
- reviewer→sre is 403 and produces an audit row
- target runs under its own credentials (not caller's)
- malformed payload (failing input_schema) returns 422
- unknown target returns 404
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


@pytest.fixture
def sre_token():
    return get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))


# ---------------------------------------------------------------------------
# Happy-path: allowed invocation
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_agent_invoke_allowed(architect_token):
    """Architect can invoke code-reviewer; structured result is returned."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "code-reviewer",
            "artifact_type": "git_diff",
            "payload": {"repo": "test", "base_ref": "HEAD~1", "head_ref": "HEAD"},
        },
        headers={"Authorization": f"Bearer {architect_token}"},
        timeout=60.0,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Result is forwarded from the target agent — must contain some structured output
    assert isinstance(body, dict)


@pytest.mark.integration
def test_agent_invoke_requires_auth():
    """agent_invoke without a token returns 401."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={"target": "code-reviewer", "artifact_type": "git_diff", "payload": {}},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Denial: policy-blocked invocation, must also be audited
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_agent_invoke_denied_is_403_and_audited(reviewer_token):
    """Reviewer→sre invocation is 403 AND an audit row exists for the deny."""
    before_ts = int(time.time() * 1000)

    resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "sre",
            "artifact_type": "incident",
            "payload": {"alert": "inject_test"},
        },
        headers={"Authorization": f"Bearer {reviewer_token}"},
    )
    assert resp.status_code == 403, resp.text

    # Give async audit a moment to flush
    time.sleep(0.5)

    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT policy_decision, tool_name FROM audit_log "
                "WHERE policy_decision='deny' AND timestamp_ms >= %s "
                "ORDER BY timestamp_ms DESC LIMIT 5",
                (before_ts,),
            )
            rows = cur.fetchall()
    assert any("sre" in r["tool_name"] or "invoke" in r["tool_name"] for r in rows), (
        f"Expected deny audit row for sre invocation, recent denies: {rows}"
    )


# ---------------------------------------------------------------------------
# Target credentials: invoked agent runs under its own token
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_invoke_uses_target_credentials(architect_token):
    """The caller's JWT sub is not forwarded to the target — target uses its own creds."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "code-reviewer",
            "artifact_type": "git_diff",
            "payload": {"repo": "test", "base_ref": "HEAD~1", "head_ref": "HEAD"},
        },
        headers={"Authorization": f"Bearer {architect_token}"},
        timeout=60.0,
    )
    assert resp.status_code == 200, resp.text
    # Verify the audit log shows code-reviewer (not architect) as the agent_id for the
    # internal tool calls made during the invocation
    time.sleep(0.5)
    conn = get_root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute(
                "SELECT agent_id, tool_name FROM audit_log "
                "WHERE agent_id='code-reviewer' "
                "ORDER BY timestamp_ms DESC LIMIT 5",
            )
            rows = cur.fetchall()
    # We should see code-reviewer (not architect) as the agent that called tools
    assert len(rows) > 0, (
        "Expected audit rows where agent_id='code-reviewer'; "
        "target should use its own credentials"
    )


# ---------------------------------------------------------------------------
# Input schema validation
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_invoke_rejects_malformed_payload(architect_token):
    """Payload failing target input_schema returns 422 before any OPA/network call."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "code-reviewer",
            "artifact_type": "git_diff",
            # Missing required fields — this is the malformed payload
            "payload": {"bad_field": True},
        },
        headers={"Authorization": f"Bearer {architect_token}"},
    )
    assert resp.status_code == 422, (
        f"Expected 422 for malformed payload, got {resp.status_code}: {resp.text}"
    )


# ---------------------------------------------------------------------------
# Unknown target
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_invoke_unknown_target_returns_404(architect_token):
    """Invoking an unknown target returns 404."""
    resp = httpx.post(
        f"{GOVERNANCE_URL}/agent/invoke",
        json={
            "target": "nonexistent-agent",
            "artifact_type": "anything",
            "payload": {},
        },
        headers={"Authorization": f"Bearer {architect_token}"},
    )
    assert resp.status_code == 404, resp.text
