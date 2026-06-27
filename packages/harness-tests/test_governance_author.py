"""Governance: POST /skills/author and GET /skills/{id}/prompt — issue 01.

Tests:
- POST /skills/author returns 201 with skill_id, status=active, version=1
- authored skill has manually_authored=1 in Dolt
- authored skill has expires_at ≈ now+90d
- authored skill appears in POST /skills/select results
- POST /skills/author produces a Dolt commit
- agent token (sre) calling POST /skills/author gets 403
- GET /skills/{id}/prompt returns prompt_template for active authored skill
- GET /skills/{id}/prompt returns 410 for a revoked skill
- GET /skills/{id}/prompt returns 404 for unknown skill
- GET /skills/{id}/prompt succeeds with any valid JWT
"""

import os
from datetime import datetime, timedelta, timezone

import httpx
import pymysql
import pymysql.cursors
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))

pytestmark = pytest.mark.integration

_AUTHOR_PAYLOAD = {
    "name": "test-triage-db-latency",
    "agent_role": "sre",
    "description": "Triage high DB latency by checking slow query log and connection pool.",
    "prompt_template": "You are an SRE. When DB latency is high, check slow queries first.",
    "steps": [
        {"action": "observability_query", "params": {}, "on_failure": "ABORT"},
        {"action": "log_search", "params": {}, "on_failure": "CONTINUE"},
    ],
    "preconditions": {
        "env_constraints": {"env": "production"},
        "task_patterns": [".*db.*latency.*", ".*slow.*query.*"],
    },
    "input_schema": {"type": "object"},
    "output_contract": {"type": "object"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_token(client_id: str, secret: str | None = None) -> str:
    if secret is None:
        secret = os.environ.get(
            f"{client_id.upper().replace('-', '_')}_SECRET", f"{client_id}-secret"
        )
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _operator_token() -> str:
    return _get_token("human-operator", os.environ.get("HUMAN_OPERATOR_SECRET", "human-operator-secret"))


def _sre_token() -> str:
    return _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))


def _root_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root",
        database="harness", connect_timeout=5, autocommit=True,
    )


def _author_skill(payload: dict | None = None) -> tuple[str, httpx.Response]:
    body = payload if payload is not None else _AUTHOR_PAYLOAD
    token = _operator_token()
    resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/author",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    skill_id = resp.json().get("skill_id") if resp.status_code == 201 else None
    return skill_id, resp


def _revoke_skill(skill_id: str) -> None:
    token = _operator_token()
    httpx.post(
        f"{GOVERNANCE_URL}/skills/{skill_id}/revoke",
        json={"reason": "test cleanup"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# POST /skills/author
# ---------------------------------------------------------------------------


def test_author_skill_returns_201():
    skill_id, resp = _author_skill()
    assert resp.status_code == 201
    body = resp.json()
    assert "skill_id" in body
    assert body["status"] == "active"
    assert body["version"] == 1


def test_authored_skill_manually_authored_flag():
    skill_id, resp = _author_skill()
    assert resp.status_code == 201

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT manually_authored FROM skills WHERE id=%s LIMIT 1", (skill_id,))
            row = cur.fetchone()
    assert row is not None
    assert row["manually_authored"] == 1


def test_authored_skill_expires_at_90d():
    skill_id, resp = _author_skill()
    assert resp.status_code == 201

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT expires_at FROM skills WHERE id=%s LIMIT 1", (skill_id,))
            row = cur.fetchone()
    assert row is not None
    expires_at = row["expires_at"]
    now = datetime.utcnow()
    assert timedelta(days=88) < (expires_at - now) < timedelta(days=92)


def test_authored_skill_selectable():
    skill_id, resp = _author_skill(
        {**_AUTHOR_PAYLOAD, "name": "test-selectable", "preconditions": {}}
    )
    assert resp.status_code == 201

    token = _operator_token()
    sel = httpx.post(
        f"{GOVERNANCE_URL}/skills/select",
        json={"env_fingerprint": {}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert sel.status_code == 200
    body = sel.json()
    selected_ids = [body["selected"]] if body.get("selected") else [t["id"] for t in body.get("tied_skills", [])]
    assert skill_id in selected_ids


def test_author_skill_produces_dolt_commit():
    _, resp = _author_skill()
    assert resp.status_code == 201
    skill_id = resp.json()["skill_id"]

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT message FROM dolt_log LIMIT 5")
            messages = [r["message"] for r in cur.fetchall()]
    assert any("author" in m and skill_id[:8] in m for m in messages)


def test_author_skill_agent_token_403():
    token = _sre_token()
    resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/author",
        json=_AUTHOR_PAYLOAD,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /skills/{id}/prompt
# ---------------------------------------------------------------------------


def test_get_prompt_returns_prompt_template():
    skill_id, author_resp = _author_skill()
    assert author_resp.status_code == 201

    token = _sre_token()
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills/{skill_id}/prompt",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["skill_id"] == skill_id
    assert body["prompt_template"] == _AUTHOR_PAYLOAD["prompt_template"]


def test_get_prompt_410_for_revoked():
    skill_id, _ = _author_skill()
    _revoke_skill(skill_id)

    token = _operator_token()
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills/{skill_id}/prompt",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 410


def test_get_prompt_404_for_unknown():
    token = _operator_token()
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills/nonexistent-skill-xyz/prompt",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 404


def test_get_prompt_any_valid_jwt():
    skill_id, _ = _author_skill()

    for client_id in ("sre", "human-operator"):
        token = _get_token(client_id)
        resp = httpx.get(
            f"{GOVERNANCE_URL}/skills/{skill_id}/prompt",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10.0,
        )
        assert resp.status_code == 200, f"expected 200 for {client_id}, got {resp.status_code}"
