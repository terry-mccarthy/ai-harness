"""Outcome labeling endpoint — issue 03.

Tests POST /episodes/{episode_id}/label:
- happy path (different principal, valid signal → 200 + Dolt commit)
- same-principal self-label → 409
- empty outcome_signal → 422
- re-labeling already-labeled episode → 409
- principal without episode:label scope → 403
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
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))

VALID_OUTCOMES = {"RESOLVED", "FAILED", "ROLLED_BACK", "HUMAN_OVERRIDE", "INCONCLUSIVE"}


def _get_token(client_id: str, client_secret: str) -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _root_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root",
        database="harness", connect_timeout=5, autocommit=True,
    )


def _insert_episode(agent_principal: str, outcome=None, outcome_labeled_at=None) -> str:
    """Insert a bare episode row for test setup; returns episode_id."""
    episode_id = str(uuid.uuid4())
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            if outcome is not None:
                cur.execute(
                    "INSERT INTO episodes (episode_id, agent_principal, outcome, outcome_labeled_at) "
                    "VALUES (%s, %s, %s, NOW())",
                    (episode_id, agent_principal, outcome),
                )
            else:
                cur.execute(
                    "INSERT INTO episodes (episode_id, agent_principal) VALUES (%s, %s)",
                    (episode_id, agent_principal),
                )
        conn.commit()
    return episode_id


def _label(token: str, episode_id: str, body: dict) -> httpx.Response:
    return httpx.post(
        f"{GOVERNANCE_URL}/episodes/{episode_id}/label",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_label_returns_200_and_commits():
    """Different principal labels an episode → 200, episode updated in Dolt."""
    episode_id = _insert_episode("sre")
    token = _get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])

    resp = _label(token, episode_id, {
        "outcome": "RESOLVED",
        "outcome_signal": {"latency_p99_ms": 120, "error_rate": 0.0},
        "labeler_principal": "code-reviewer",
    })
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["outcome"] == "RESOLVED"
    assert data["outcome_labeled_at"] is not None

    # verify Dolt row
    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT outcome, outcome_labeled_at, human_actor FROM episodes WHERE episode_id=%s", (episode_id,))
            row = cur.fetchone()
    assert row["outcome"] == "RESOLVED"
    assert row["outcome_labeled_at"] is not None


@pytest.mark.integration
def test_dolt_commit_created_on_label():
    """Labeling creates a Dolt commit with the episode id and outcome."""
    episode_id = _insert_episode("sre")
    token = _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))

    # sre labels a different agent's episode
    episode_id2 = _insert_episode("architect")
    _label(token, episode_id2, {
        "outcome": "FAILED",
        "outcome_signal": {"error_rate": 0.42},
        "labeler_principal": "sre",
    })

    time.sleep(1)

    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("SELECT message FROM dolt_log LIMIT 10")
            messages = [r[0] for r in cur.fetchall()]
    assert any("labeled" in m and episode_id2[:8] in m for m in messages), (
        f"No labeling commit found. Recent log: {messages}"
    )


# ---------------------------------------------------------------------------
# Rejection cases
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_self_label_returns_409():
    """Same principal as agent_principal attempting to label → 409."""
    episode_id = _insert_episode("sre")
    token = _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))

    resp = _label(token, episode_id, {
        "outcome": "RESOLVED",
        "outcome_signal": {"ok": True},
        "labeler_principal": "sre",
    })
    assert resp.status_code == 409, resp.text


@pytest.mark.integration
def test_empty_outcome_signal_returns_422():
    """Empty outcome_signal → 422 (self-declared-success antipattern)."""
    episode_id = _insert_episode("sre")
    token = _get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])

    resp = _label(token, episode_id, {
        "outcome": "RESOLVED",
        "outcome_signal": {},
        "labeler_principal": "code-reviewer",
    })
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
def test_relabel_returns_409():
    """Re-labeling an already-labeled episode → 409."""
    episode_id = _insert_episode("sre", outcome="RESOLVED", outcome_labeled_at="now")
    token = _get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])

    resp = _label(token, episode_id, {
        "outcome": "FAILED",
        "outcome_signal": {"error_rate": 0.1},
        "labeler_principal": "code-reviewer",
    })
    assert resp.status_code == 409, resp.text


@pytest.mark.integration
def test_opa_rejects_no_label_scope():
    """architect role has no episode:label scope → 403."""
    episode_id = _insert_episode("sre")
    token = _get_token("architect", os.environ.get("ARCHITECT_SECRET", "architect-secret"))

    resp = _label(token, episode_id, {
        "outcome": "RESOLVED",
        "outcome_signal": {"ok": True},
        "labeler_principal": "architect",
    })
    assert resp.status_code == 403, resp.text


@pytest.mark.integration
def test_missing_episode_returns_404():
    """Unknown episode_id → 404."""
    token = _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))
    resp = _label(token, str(uuid.uuid4()), {
        "outcome": "RESOLVED",
        "outcome_signal": {"ok": True},
        "labeler_principal": "sre",
    })
    assert resp.status_code == 404, resp.text
