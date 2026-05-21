"""Phase 1 Governance tests — all @pytest.mark.integration.

Red phase: these tests are written before the implementation exists.
They will fail until governance service, Dolt, architect/sre stubs, and OPA
policy updates are all in place.
"""

import os
import time
import pytest
import httpx
import pymysql
import jwt

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
OPA_URL = os.environ.get("OPA_URL", "http://localhost:8181")

DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))


def get_token(client_id: str, client_secret: str) -> str:
    """Helper: fetch a bearer token from the governance /oauth/token endpoint."""
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


def invoke_tool(token: str, tool_name: str, params: dict) -> httpx.Response:
    """Helper: call POST /api/v0/tools/invoke on the governance service."""
    return httpx.post(
        f"{GOVERNANCE_URL}/api/v0/tools/invoke",
        json={"name": tool_name, **params},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )


def get_dolt_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user="harness",
        password="harness",
        database="harness",
        autocommit=True,
    )


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_architect_client_auth():
    """POST /oauth/token with architect credentials returns access_token."""
    architect_secret = os.environ.get("ARCHITECT_SECRET", "architect-secret")
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "architect",
            "client_secret": architect_secret,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data
    assert data.get("token_type", "").lower() == "bearer"


@pytest.mark.integration
def test_reviewer_client_auth():
    """POST /oauth/token with code-reviewer credentials returns access_token."""
    reviewer_secret = os.environ["CODE_REVIEWER_SECRET"]
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "code-reviewer",
            "client_secret": reviewer_secret,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


@pytest.mark.integration
def test_sre_client_auth():
    """POST /oauth/token with sre credentials returns access_token."""
    sre_secret = os.environ.get("SRE_SECRET", "sre-secret")
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "sre",
            "client_secret": sre_secret,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "access_token" in data


# ---------------------------------------------------------------------------
# Tool invocation policy tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_architect_allowed_tool():
    """Architect token can call codebase_search via governance proxy."""
    architect_secret = os.environ.get("ARCHITECT_SECRET", "architect-secret")
    token = get_token("architect", architect_secret)
    resp = invoke_tool(token, "architect_stub__codebase_search", {"query": "auth module"})
    assert resp.status_code == 200


@pytest.mark.integration
def test_architect_denied_tool():
    """Architect token calling shell_exec returns 403."""
    architect_secret = os.environ.get("ARCHITECT_SECRET", "architect-secret")
    token = get_token("architect", architect_secret)
    resp = invoke_tool(token, "sre_stub__shell_exec", {"command": "ls"})
    assert resp.status_code == 403


@pytest.mark.integration
def test_reviewer_allowed_tool():
    """code-reviewer token can call git_diff via governance proxy, returns result."""
    reviewer_secret = os.environ["CODE_REVIEWER_SECRET"]
    token = get_token("code-reviewer", reviewer_secret)
    resp = invoke_tool(token, "git_diff_stub__git_diff", {"diff_text": "test diff"})
    assert resp.status_code == 200
    data = resp.json()
    assert data is not None


@pytest.mark.integration
def test_reviewer_denied_tool():
    """code-reviewer token calling adr_write returns 403."""
    reviewer_secret = os.environ["CODE_REVIEWER_SECRET"]
    token = get_token("code-reviewer", reviewer_secret)
    resp = invoke_tool(token, "architect_stub__adr_write", {"title": "test", "content": "test"})
    assert resp.status_code == 403


@pytest.mark.integration
def test_sre_allowed_tool():
    """sre token can call runbook_read via governance proxy, returns result."""
    sre_secret = os.environ.get("SRE_SECRET", "sre-secret")
    token = get_token("sre", sre_secret)
    resp = invoke_tool(token, "sre_stub__runbook_read", {"runbook_name": "incident-response"})
    assert resp.status_code == 200
    data = resp.json()
    assert data is not None


@pytest.mark.integration
def test_unknown_token_rejected():
    """Request with 'Bearer invalid-token' returns 401."""
    resp = invoke_tool("invalid-token", "git_diff_stub__git_diff", {"diff_text": "test"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Audit / Dolt tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_audit_row_written():
    """After any tool call, a row exists in audit_log table (Dolt via MySQL)."""
    # Make a tool call to ensure at least one audit row
    reviewer_secret = os.environ["CODE_REVIEWER_SECRET"]
    token = get_token("code-reviewer", reviewer_secret)
    invoke_tool(token, "git_diff_stub__git_diff", {"diff_text": "audit test diff"})

    conn = get_dolt_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM audit_log")
            (count,) = cur.fetchone()
        assert count > 0, "No audit rows found in audit_log"
    finally:
        conn.close()


@pytest.mark.integration
def test_audit_policy_rule_recorded():
    """audit_log row has policy_decision and policy_rule fields populated."""
    reviewer_secret = os.environ["CODE_REVIEWER_SECRET"]
    token = get_token("code-reviewer", reviewer_secret)
    invoke_tool(token, "git_diff_stub__git_diff", {"diff_text": "policy rule test"})

    conn = get_dolt_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT policy_decision, policy_rule FROM audit_log "
                "ORDER BY timestamp_ms DESC LIMIT 1"
            )
            row = cur.fetchone()
        assert row is not None, "No audit rows found"
        policy_decision, policy_rule = row
        assert policy_decision in ("allow", "deny")
        assert policy_rule is not None and len(policy_rule) > 0
    finally:
        conn.close()


@pytest.mark.integration
def test_audit_dolt_commit_created():
    """After audit INSERT, DOLT_LOG() shows a new commit with the tool name."""
    reviewer_secret = os.environ["CODE_REVIEWER_SECRET"]
    token = get_token("code-reviewer", reviewer_secret)
    invoke_tool(token, "git_diff_stub__git_diff", {"diff_text": "dolt commit test"})

    conn = get_dolt_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT message FROM dolt_log LIMIT 5")
            rows = cur.fetchall()
        messages = [r[0] for r in rows]
        # At least one commit message should mention "audit:"
        assert any("audit:" in msg for msg in messages), (
            f"No audit commit found in dolt_log. Messages: {messages}"
        )
    finally:
        conn.close()


@pytest.mark.integration
def test_audit_dolt_history_queryable():
    """SELECT * FROM dolt_diff_audit_log returns at least one row after a commit."""
    reviewer_secret = os.environ["CODE_REVIEWER_SECRET"]
    token = get_token("code-reviewer", reviewer_secret)
    invoke_tool(token, "git_diff_stub__git_diff", {"diff_text": "history queryable test"})

    conn = get_dolt_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dolt_diff_audit_log LIMIT 5")
            (count,) = cur.fetchone()
        assert count > 0, "dolt_diff_audit_log returned no rows"
    finally:
        conn.close()


@pytest.mark.integration
def test_audit_no_delete():
    """DELETE FROM audit_log raises an error (harness user has INSERT only, no DELETE)."""
    conn = get_dolt_conn()
    try:
        with pytest.raises(Exception, match="DELETE|command denied|1142"):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM audit_log WHERE 1=0")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# OPA unit tests (direct HTTP to OPA)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_opa_allow_architect_tool():
    """OPA POST /v1/data/harness/allow with {architect, codebase_search} returns true."""
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/allow",
        json={"input": {"agent_role": "architect", "tool_name": "codebase_search"}},
        timeout=5.0,
    )
    assert resp.status_code == 200
    assert resp.json().get("result") is True


@pytest.mark.integration
def test_opa_deny_cross_role():
    """OPA POST /v1/data/harness/allow with {architect, shell_exec} returns false."""
    resp = httpx.post(
        f"{OPA_URL}/v1/data/harness/allow",
        json={"input": {"agent_role": "architect", "tool_name": "shell_exec"}},
        timeout=5.0,
    )
    assert resp.status_code == 200
    assert resp.json().get("result") is not True


# ---------------------------------------------------------------------------
# Token expiry test
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_token_expiry():
    """Forge a JWT with exp in the past; governance returns 401."""
    jwt_secret = os.environ.get("JWT_SECRET", "dev-jwt-secret-change-in-prod")
    expired = jwt.encode(
        {
            "sub": "code-reviewer",
            "role": "code_reviewer",
            "iat": int(time.time()) - 1000,
            "exp": int(time.time()) - 100,
        },
        jwt_secret,
        algorithm="HS256",
    )
    resp = invoke_tool(expired, "git_diff_stub__git_diff", {"diff_text": "expiry test"})
    assert resp.status_code == 401
