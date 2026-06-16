"""Skills CLI — integration tests.

Covers:
- GET /episodes, GET /candidates, GET /skills list endpoints (new)
- CLI subprocess: token, pipeline, episodes list/label, candidates propose/promote, skills select
"""

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pymysql
import pymysql.cursors
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))
CLI_PATH = Path(__file__).parent.parent.parent / "scripts" / "skills_cli.py"

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_token(client_id: str, secret: str | None = None) -> str:
    if secret is None:
        secret = os.environ.get(f"{client_id.upper().replace('-', '_')}_SECRET", f"{client_id}-secret")
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _root_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root",
        database="harness", connect_timeout=5, autocommit=True,
    )


def _insert_episode(agent_principal: str, outcome: str | None = None, recent: bool = True) -> str:
    episode_id = str(uuid.uuid4())
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            if outcome:
                labeled_at = (datetime.now(timezone.utc) - timedelta(days=5 if recent else 200)).replace(tzinfo=None)
                cur.execute(
                    "INSERT INTO episodes (episode_id, agent_principal, outcome, outcome_labeled_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (episode_id, agent_principal, outcome, labeled_at),
                )
            else:
                cur.execute(
                    "INSERT INTO episodes (episode_id, agent_principal) VALUES (%s, %s)",
                    (episode_id, agent_principal),
                )
    return episode_id


def _make_qualified_episode_ids() -> list[str]:
    return [
        _insert_episode("sre", outcome="RESOLVED"),
        _insert_episode("sre", outcome="RESOLVED"),
        _insert_episode("sre", outcome="RESOLVED"),
        _insert_episode("code-reviewer", outcome="RESOLVED"),
        _insert_episode("code-reviewer", outcome="RESOLVED"),
    ]


def _propose_candidate(episode_ids: list[str], cluster_key: str | None = None) -> str:
    token = _get_token("sre")
    ck = cluster_key or f"cli.test-{uuid.uuid4().hex[:6]}"
    resp = httpx.post(
        f"{GOVERNANCE_URL}/candidates",
        headers={"Authorization": f"Bearer {token}"},
        json={"episode_ids": episode_ids, "cluster_key": ck, "proposed_procedure": {"steps": ["triage"]}},
    )
    resp.raise_for_status()
    return resp.json()["candidate_id"]


def _cli(*args, client: str = "sre") -> dict | list:
    """Run the CLI and return parsed JSON output."""
    secret = os.environ.get(f"{client.upper().replace('-', '_')}_SECRET", f"{client}-secret")
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), "--url", GOVERNANCE_URL, "--client", client, "--secret", secret, *args],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"CLI exited {result.returncode}: {result.stderr}"
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# GET /episodes list endpoint
# ---------------------------------------------------------------------------

def test_list_episodes_returns_list():
    _insert_episode("sre")
    token = _get_token("sre")
    resp = httpx.get(
        f"{GOVERNANCE_URL}/episodes",
        headers={"Authorization": f"Bearer {token}"},
        params={"limit": 5},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) <= 5


def test_list_episodes_unlabeled_filter():
    labeled_id = _insert_episode("sre", outcome="RESOLVED")
    unlabeled_id = _insert_episode("sre")
    token = _get_token("sre")
    resp = httpx.get(
        f"{GOVERNANCE_URL}/episodes",
        headers={"Authorization": f"Bearer {token}"},
        params={"unlabeled": "true", "limit": 100},
    )
    assert resp.status_code == 200
    ids = {r["episode_id"] for r in resp.json()}
    assert unlabeled_id in ids
    assert labeled_id not in ids


def test_list_episodes_requires_auth():
    resp = httpx.get(f"{GOVERNANCE_URL}/episodes")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /candidates list endpoint
# ---------------------------------------------------------------------------

def test_list_candidates_returns_list():
    token = _get_token("sre")
    resp = httpx.get(
        f"{GOVERNANCE_URL}/candidates",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_candidates_status_filter():
    eids = _make_qualified_episode_ids()
    candidate_id = _propose_candidate(eids)
    token = _get_token("sre")

    proposed = httpx.get(
        f"{GOVERNANCE_URL}/candidates",
        headers={"Authorization": f"Bearer {token}"},
        params={"status": "PROPOSED"},
    )
    assert proposed.status_code == 200
    ids = {r["candidate_id"] for r in proposed.json()}
    assert candidate_id in ids

    rejected = httpx.get(
        f"{GOVERNANCE_URL}/candidates",
        headers={"Authorization": f"Bearer {token}"},
        params={"status": "REJECTED"},
    )
    assert candidate_id not in {r["candidate_id"] for r in rejected.json()}


def test_list_candidates_requires_auth():
    resp = httpx.get(f"{GOVERNANCE_URL}/candidates")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /skills list endpoint
# ---------------------------------------------------------------------------

def test_list_skills_returns_list():
    token = _get_token("sre")
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_skills_status_filter():
    eids = _make_qualified_episode_ids()
    ck = f"cli.skill-{uuid.uuid4().hex[:6]}"
    cid = _propose_candidate(eids, cluster_key=ck)
    hop_token = _get_token("human-operator")
    httpx.post(
        f"{GOVERNANCE_URL}/candidates/{cid}/promote",
        headers={"Authorization": f"Bearer {hop_token}"},
        json={},
    ).raise_for_status()

    token = _get_token("sre")
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills",
        headers={"Authorization": f"Bearer {token}"},
        params={"status": "active"},
    )
    assert resp.status_code == 200
    ids = {r["id"] for r in resp.json()}
    assert ck in ids


def test_list_skills_requires_auth():
    resp = httpx.get(f"{GOVERNANCE_URL}/skills")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# CLI: token command
# ---------------------------------------------------------------------------

def test_cli_token_returns_access_token():
    result = subprocess.run(
        [sys.executable, str(CLI_PATH), "--url", GOVERNANCE_URL, "token", "--client", "sre", "--secret", "sre-secret"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    data = json.loads(result.stdout)
    assert "access_token" in data
    assert data["token_type"] == "bearer"


# ---------------------------------------------------------------------------
# CLI: pipeline summary
# ---------------------------------------------------------------------------

def test_cli_pipeline_shows_summary():
    data = _cli("pipeline")
    assert "episodes" in data
    assert "candidates" in data
    assert "skills" in data
    assert "total" in data["episodes"]
    assert "unlabeled" in data["episodes"]


# ---------------------------------------------------------------------------
# CLI: episodes
# ---------------------------------------------------------------------------

def test_cli_episodes_list():
    _insert_episode("sre")
    data = _cli("episodes", "list", "--limit", "5")
    assert isinstance(data, list)


def test_cli_episodes_label():
    # Episode by "code-reviewer"; labeler is "sre" (different principal, sre role allowed by OPA)
    episode_id = _insert_episode("code-reviewer")
    data = _cli(
        "episodes", "label", episode_id,
        "--outcome", "RESOLVED",
        "--signal", '{"metric": "p99", "value": 10}',
        "--labeler", "sre",
        client="sre",
    )
    assert data["outcome"] == "RESOLVED"
    assert data["episode_id"] == episode_id


# ---------------------------------------------------------------------------
# CLI: candidates
# ---------------------------------------------------------------------------

def test_cli_candidates_list():
    data = _cli("candidates", "list")
    assert isinstance(data, list)


def test_cli_candidates_propose():
    eids = _make_qualified_episode_ids()
    ck = f"cli.prop-{uuid.uuid4().hex[:6]}"
    data = _cli(
        "candidates", "propose",
        "--cluster-key", ck,
        "--episodes", ",".join(eids),
        client="sre",
    )
    assert data["status"] == "PROPOSED"
    assert "candidate_id" in data


def test_cli_candidates_promote():
    eids = _make_qualified_episode_ids()
    ck = f"cli.promo-{uuid.uuid4().hex[:6]}"
    cid = _propose_candidate(eids, cluster_key=ck)
    data = _cli("candidates", "promote", cid, client="human-operator")
    assert "skill_id" in data
    assert data["skill_id"] == ck


# ---------------------------------------------------------------------------
# CLI: skills
# ---------------------------------------------------------------------------

def test_cli_skills_list():
    data = _cli("skills", "list")
    assert isinstance(data, list)


def test_cli_skills_select():
    data = _cli("skills", "select")
    assert "selected" in data


def test_cli_skills_revoke():
    eids = _make_qualified_episode_ids()
    ck = f"cli.rev-{uuid.uuid4().hex[:6]}"
    cid = _propose_candidate(eids, cluster_key=ck)
    _cli("candidates", "promote", cid, client="human-operator")
    data = _cli("skills", "revoke", ck, "--reason", "cli-test revoke", client="human-operator")
    assert data["status"] == "revoked"
