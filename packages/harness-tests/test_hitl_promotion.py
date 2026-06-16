"""HITL promotion gate — issue 05.

Tests POST /candidates/{id}/promote and POST /candidates/{id}/reject:
- happy path: human-operator promotes → ACTIVE skill row, 90-day expiry, Dolt commit
- re-promotion: new version + procedure diff in response
- reject: reason stored, candidate REJECTED
- agent-role tokens (sre, code_reviewer, architect) → 403
- promote already-promoted candidate → 409
- reject already-rejected candidate → 409
- reject without reason → 422
- full episode → candidate → promote integration flow
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pymysql
import pymysql.cursors
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
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


def _now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _insert_episode(agent_principal: str, recent: bool = True) -> str:
    episode_id = str(uuid.uuid4())
    labeled_at = _now_utc() - timedelta(days=5 if recent else 100)
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes (episode_id, agent_principal, outcome, outcome_labeled_at) "
                "VALUES (%s, %s, 'RESOLVED', %s)",
                (episode_id, agent_principal, labeled_at),
            )
        conn.commit()
    return episode_id


def _make_qualified_episode_ids() -> list[str]:
    """5 RESOLVED+labeled episodes across 2 principals, 3 recent."""
    return [
        _insert_episode("sre"),
        _insert_episode("sre"),
        _insert_episode("sre"),
        _insert_episode("code-reviewer"),
        _insert_episode("code-reviewer"),
    ]


def _propose_candidate(episode_ids: list, cluster_key: str = None, procedure: dict = None) -> str:
    """POST /candidates with sre token; return candidate_id."""
    token = _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))
    resp = httpx.post(
        f"{GOVERNANCE_URL}/candidates",
        json={
            "episode_ids": episode_ids,
            "cluster_key": cluster_key or f"sre.test:{uuid.uuid4().hex[:8]}",
            "proposed_procedure": procedure or {"steps": ["observe", "query", "remediate"]},
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["candidate_id"]


def _promote(token: str, candidate_id: str, body: dict = None) -> httpx.Response:
    return httpx.post(
        f"{GOVERNANCE_URL}/candidates/{candidate_id}/promote",
        json=body or {},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )


def _reject(token: str, candidate_id: str, reason: str = "not ready") -> httpx.Response:
    return httpx.post(
        f"{GOVERNANCE_URL}/candidates/{candidate_id}/reject",
        json={"reason": reason},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# Happy path — promote
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_promote_creates_active_skill():
    """human-operator promotes a candidate → ACTIVE skill with non-null promoted_by and expiry."""
    ids = _make_qualified_episode_ids()
    cluster_key = f"sre.test:{uuid.uuid4().hex[:8]}"
    cid = _propose_candidate(ids, cluster_key)

    resp = _promote(_human_token(), cid)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["skill_id"] == cluster_key
    assert data["version"] == 1

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM skills WHERE id=%s", (cluster_key,))
            skill = cur.fetchone()

    assert skill is not None
    assert skill["status"] == "active"
    assert skill["promoted_by"] == "human-operator"
    assert skill["expires_at"] is not None


@pytest.mark.integration
def test_promote_transitions_candidate_to_promoted():
    """Candidate status becomes PROMOTED after promotion."""
    ids = _make_qualified_episode_ids()
    cid = _propose_candidate(ids)
    _promote(_human_token(), cid)

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT status FROM candidates WHERE candidate_id=%s", (cid,))
            row = cur.fetchone()
    assert row["status"] == "PROMOTED"


@pytest.mark.integration
def test_promote_dolt_commit_message():
    """Promotion creates a Dolt commit referencing the candidate and human principal."""
    ids = _make_qualified_episode_ids()
    cid = _propose_candidate(ids)
    _promote(_human_token(), cid)

    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT message FROM dolt_log LIMIT 10")
            messages = [r[0] for r in cur.fetchall()]
    assert any("human-operator" in m and cid[:8] in m for m in messages), (
        f"No promotion commit found. Messages: {messages}"
    )


@pytest.mark.integration
def test_promote_skill_expires_90_days_out():
    """expires_at is approximately NOW() + 90 days."""
    ids = _make_qualified_episode_ids()
    cluster_key = f"sre.test:{uuid.uuid4().hex[:8]}"
    cid = _propose_candidate(ids, cluster_key)
    _promote(_human_token(), cid)

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT expires_at FROM skills WHERE id=%s", (cluster_key,))
            row = cur.fetchone()

    expires = row["expires_at"]
    delta = expires - _now_utc()
    assert 88 <= delta.days <= 91, f"expected ~90 days, got {delta.days}"


# ---------------------------------------------------------------------------
# Re-promotion — new version + diff
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_repromote_increments_version():
    """Re-promoting the same cluster_key creates version 2."""
    cluster_key = f"sre.test:{uuid.uuid4().hex[:8]}"

    ids1 = _make_qualified_episode_ids()
    cid1 = _propose_candidate(ids1, cluster_key, {"steps": ["v1-step"]})
    _promote(_human_token(), cid1)

    ids2 = _make_qualified_episode_ids()
    cid2 = _propose_candidate(ids2, cluster_key, {"steps": ["v2-step"]})
    resp = _promote(_human_token(), cid2)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["version"] == 2
    assert data["procedure_diff"] is not None


# ---------------------------------------------------------------------------
# Reject
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_reject_sets_status_rejected():
    """POST /candidates/{id}/reject with reason → candidate REJECTED."""
    ids = _make_qualified_episode_ids()
    cid = _propose_candidate(ids)
    resp = _reject(_human_token(), cid, reason="procedure unclear")
    assert resp.status_code == 200, resp.text

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT status FROM candidates WHERE candidate_id=%s", (cid,))
            row = cur.fetchone()
    assert row["status"] == "REJECTED"


@pytest.mark.integration
def test_reject_without_reason_returns_422():
    ids = _make_qualified_episode_ids()
    cid = _propose_candidate(ids)
    resp = httpx.post(
        f"{GOVERNANCE_URL}/candidates/{cid}/reject",
        json={},
        headers={"Authorization": f"Bearer {_human_token()}"},
        timeout=10.0,
    )
    assert resp.status_code == 422, resp.text


# ---------------------------------------------------------------------------
# Idempotency / double-action guards
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_repromote_already_promoted_candidate_409():
    """Promoting an already-PROMOTED candidate → 409."""
    ids = _make_qualified_episode_ids()
    cid = _propose_candidate(ids)
    _promote(_human_token(), cid)
    resp = _promote(_human_token(), cid)
    assert resp.status_code == 409, resp.text


@pytest.mark.integration
def test_reject_already_rejected_candidate_409():
    """Rejecting an already-REJECTED candidate → 409."""
    ids = _make_qualified_episode_ids()
    cid = _propose_candidate(ids)
    _reject(_human_token(), cid)
    resp = _reject(_human_token(), cid)
    assert resp.status_code == 409, resp.text


# ---------------------------------------------------------------------------
# OPA scope enforcement
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("client_id,secret_env", [
    ("architect", "ARCHITECT_SECRET"),
    ("sre", "SRE_SECRET"),
    ("code-reviewer", "CODE_REVIEWER_SECRET"),
])
def test_agent_role_cannot_promote(client_id, secret_env):
    """Agent-role tokens lack skill:promote scope → 403."""
    ids = _make_qualified_episode_ids()
    cid = _propose_candidate(ids)
    secret = os.environ.get(secret_env, f"{client_id}-secret")
    token = _get_token(client_id, secret)
    resp = _promote(token, cid)
    assert resp.status_code == 403, f"{client_id} should be denied: {resp.text}"


# ---------------------------------------------------------------------------
# Full end-to-end flow
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_full_episode_to_skill_flow():
    """episode capture → candidate proposal → human promotion → ACTIVE skill."""
    cluster_key = f"sre.e2e:{uuid.uuid4().hex[:8]}"
    ids = _make_qualified_episode_ids()
    cid = _propose_candidate(ids, cluster_key)

    resp = _promote(_human_token(), cid)
    assert resp.status_code == 200, resp.text

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT id, status, version, source_candidate_id FROM skills WHERE id=%s", (cluster_key,))
            skill = cur.fetchone()

    assert skill["status"] == "active"
    assert skill["version"] == 1
    assert skill["source_candidate_id"] == cid
