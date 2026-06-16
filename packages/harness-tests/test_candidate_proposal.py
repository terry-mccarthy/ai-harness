"""Manual candidate proposal — issue 04.

Tests POST /candidates and GET /candidates/{id}:
- happy path: 5+ qualified independent recent RESOLVED episodes → 201
- below N_min (< 5 episodes) → 422
- below K distinct principals (< 2) → 422
- below M recent episodes (< 2 within 90 days) → 422
- any episode not RESOLVED+labeled → 422
- OPA rejects principals without candidate:propose scope
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


def _root_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root",
        database="harness", connect_timeout=5, autocommit=True,
    )


def _insert_episode(
    agent_principal: str,
    outcome: str | None = None,
    outcome_labeled_at: datetime | None = None,
) -> str:
    episode_id = str(uuid.uuid4())
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            if outcome and outcome_labeled_at:
                cur.execute(
                    "INSERT INTO episodes (episode_id, agent_principal, outcome, outcome_labeled_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (episode_id, agent_principal, outcome, outcome_labeled_at),
                )
            else:
                cur.execute(
                    "INSERT INTO episodes (episode_id, agent_principal) VALUES (%s, %s)",
                    (episode_id, agent_principal),
                )
        conn.commit()
    return episode_id


def _recent() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=10)


def _stale() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=100)


def _post_candidates(token: str, episode_ids: list, cluster_key: str = "sre.test:latency") -> httpx.Response:
    return httpx.post(
        f"{GOVERNANCE_URL}/candidates",
        json={
            "episode_ids": episode_ids,
            "cluster_key": cluster_key,
            "proposed_procedure": {"steps": ["observe", "query", "remediate"]},
        },
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )


def _sre_token() -> str:
    return _get_token("sre", os.environ.get("SRE_SECRET", "sre-secret"))


def _reviewer_token() -> str:
    return _get_token("code-reviewer", os.environ["CODE_REVIEWER_SECRET"])


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_post_candidates_returns_201():
    """5 RESOLVED+labeled episodes across 2 principals, 2 recent → 201."""
    ids = [
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
    ]
    token = _sre_token()
    resp = _post_candidates(token, ids)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "candidate_id" in data


@pytest.mark.integration
def test_candidate_stored_in_dolt():
    """Created candidate has status=PROPOSED and correct support_stats."""
    ids = [
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
    ]
    token = _sre_token()
    resp = _post_candidates(token, ids)
    assert resp.status_code == 201, resp.text
    candidate_id = resp.json()["candidate_id"]

    conn = _root_conn()
    with conn:
        with conn.cursor(pymysql.cursors.DictCursor) as cur:
            cur.execute("SELECT * FROM candidates WHERE candidate_id=%s", (candidate_id,))
            row = cur.fetchone()

    assert row is not None
    assert row["status"] == "PROPOSED"
    stats = json.loads(row["support_stats"]) if isinstance(row["support_stats"], str) else row["support_stats"]
    assert stats["n_episodes"] == 5
    assert stats["n_principals"] == 2
    assert stats["recent_count"] >= 2


@pytest.mark.integration
def test_get_candidate_returns_full_record():
    """GET /candidates/{id} returns candidate with member_episode_ids."""
    ids = [
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
    ]
    token = _sre_token()
    cid = _post_candidates(token, ids).json()["candidate_id"]

    resp = httpx.get(
        f"{GOVERNANCE_URL}/candidates/{cid}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["candidate_id"] == cid
    member_ids = data["member_episode_ids"]
    if isinstance(member_ids, str):
        member_ids = json.loads(member_ids)
    assert set(member_ids) == set(ids)


# ---------------------------------------------------------------------------
# Rejection cases
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_below_n_min_returns_422():
    """Fewer than 5 episodes → 422."""
    ids = [
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
    ]
    resp = _post_candidates(_sre_token(), ids)
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
def test_below_k_principals_returns_422():
    """All episodes from same principal → 422."""
    ids = [_insert_episode("sre", "RESOLVED", _recent()) for _ in range(5)]
    resp = _post_candidates(_sre_token(), ids)
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
def test_below_m_recent_returns_422():
    """Fewer than 2 recent episodes (all > 90 days old) → 422."""
    ids = [
        _insert_episode("sre", "RESOLVED", _stale()),
        _insert_episode("sre", "RESOLVED", _stale()),
        _insert_episode("sre", "RESOLVED", _stale()),
        _insert_episode("code-reviewer", "RESOLVED", _stale()),
        _insert_episode("code-reviewer", "RESOLVED", _stale()),
    ]
    resp = _post_candidates(_sre_token(), ids)
    assert resp.status_code == 422, resp.text


@pytest.mark.integration
def test_unqualified_episodes_returns_422():
    """Including an unlabeled episode → 422 listing the bad ID."""
    ids = [
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
        _insert_episode("code-reviewer"),  # unlabeled — disqualifies
    ]
    resp = _post_candidates(_sre_token(), ids)
    assert resp.status_code == 422, resp.text
    assert ids[-1] in resp.text


@pytest.mark.integration
def test_opa_rejects_no_propose_scope():
    """architect role has no candidate:propose scope → 403."""
    ids = [
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("sre", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
        _insert_episode("code-reviewer", "RESOLVED", _recent()),
    ]
    token = _get_token("architect", os.environ.get("ARCHITECT_SECRET", "architect-secret"))
    resp = _post_candidates(token, ids)
    assert resp.status_code == 403, resp.text
