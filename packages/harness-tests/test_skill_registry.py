"""skills-registry-server MCP tools — issue 02.

Tests (all via GatewayClient → MCPJungle → skills-registry-server → governance):
- registry__list_skills returns skills list
- registry__get_skill returns full detail including prompt_template
- registry__get_skill_prompt returns prompt_template for authored skill
- registry__create_skill creates a skill, returns skill_id (operator)
- SRE calling registry__create_skill → ToolAccessDenied
- registry__revoke_skill revokes a skill (operator)
- SRE calling registry__revoke_skill → ToolAccessDenied
- registry__execute_skill runs skill steps end-to-end
- registry__label_episode labels an episode (SRE)
- SRE calling registry__promote_candidate → ToolAccessDenied
"""

import json
import os
import uuid
from datetime import datetime, timezone

import httpx
import pymysql
import pymysql.cursors
import pytest

from harness_gateway.client import GatewayClient, ToolAccessDenied

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
MCPJUNGLE_URL = os.environ.get("MCPJUNGLE_URL", "http://localhost:8080")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))

pytestmark = pytest.mark.integration

_SKILL_PAYLOAD = {
    "skill_name": "test-registry-skill",
    "agent_role": "sre",
    "description": "A registry test skill",
    "prompt_template": "You are an SRE. Investigate the alert.",
    "steps": [{"action": "observability_query", "params": {}, "on_failure": "ABORT"}],
    "preconditions": {"env_constraints": {}, "task_patterns": []},
    "input_schema": {"type": "object"},
    "output_contract": {"type": "object"},
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _operator_client() -> GatewayClient:
    return GatewayClient(
        gateway_url=MCPJUNGLE_URL,
        governance_url=GOVERNANCE_URL,
        client_id="human-operator",
        client_secret=os.environ.get("HUMAN_OPERATOR_SECRET", "human-operator-secret"),
    )


def _sre_client() -> GatewayClient:
    return GatewayClient(
        gateway_url=MCPJUNGLE_URL,
        governance_url=GOVERNANCE_URL,
        client_id="sre",
        client_secret=os.environ.get("SRE_SECRET", "sre-secret"),
    )


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


def _root_conn():
    return pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user="root", password="root",
        database="harness", connect_timeout=5, autocommit=True,
    )


def _insert_episode(agent_principal: str = "sre") -> str:
    episode_id = str(uuid.uuid4())
    conn = _root_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO episodes (episode_id, agent_principal) VALUES (%s, %s)",
                (episode_id, agent_principal),
            )
    return episode_id


# ---------------------------------------------------------------------------
# Tracer bullet: registry__list_skills
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_list_skills():
    client = _operator_client()
    result = await client.call_tool("registry_list_skills", {"status_filter": "active"})
    assert "skills" in result
    assert isinstance(result["skills"], list)


# ---------------------------------------------------------------------------
# registry__get_skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_get_skill():
    # Author a skill first so we have a known skill_id with prompt_template
    token = _get_token("human-operator")
    author_resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/author",
        json={**_SKILL_PAYLOAD, "name": _SKILL_PAYLOAD["skill_name"]},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert author_resp.status_code == 201
    skill_id = author_resp.json()["skill_id"]

    client = _operator_client()
    result = await client.call_tool("registry_get_skill", {"skill_id": skill_id})
    assert result["id"] == skill_id
    assert result["prompt_template"] == _SKILL_PAYLOAD["prompt_template"]


# ---------------------------------------------------------------------------
# registry__get_skill_prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_get_skill_prompt():
    token = _get_token("human-operator")
    author_resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/author",
        json={**_SKILL_PAYLOAD, "name": _SKILL_PAYLOAD["skill_name"]},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    skill_id = author_resp.json()["skill_id"]

    client = _operator_client()
    result = await client.call_tool("registry_get_skill_prompt", {"skill_id": skill_id})
    assert result["skill_id"] == skill_id
    assert result["prompt_template"] == _SKILL_PAYLOAD["prompt_template"]


# ---------------------------------------------------------------------------
# registry__create_skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_create_skill():
    client = _operator_client()
    result = await client.call_tool("registry_create_skill", {**_SKILL_PAYLOAD})
    assert "skill_id" in result
    assert result["status"] == "active"


@pytest.mark.asyncio
async def test_registry_create_skill_requires_operator():
    client = _sre_client()
    with pytest.raises(ToolAccessDenied):
        await client.call_tool("registry_create_skill", {**_SKILL_PAYLOAD})


# ---------------------------------------------------------------------------
# registry__revoke_skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_revoke_skill():
    # Create a skill to revoke
    op = _operator_client()
    created = await op.call_tool("registry_create_skill", {**_SKILL_PAYLOAD})
    skill_id = created["skill_id"]

    result = await op.call_tool("registry_revoke_skill", {"skill_id": skill_id, "reason": "test revoke"})
    assert result["status"] == "revoked"


@pytest.mark.asyncio
async def test_registry_revoke_requires_operator():
    client = _sre_client()
    with pytest.raises(ToolAccessDenied):
        await client.call_tool("registry_revoke_skill", {"skill_id": "any-id", "reason": "test"})


# ---------------------------------------------------------------------------
# registry__execute_skill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_execute_skill():
    # Insert a skill with a step the SRE role can call
    conn = _root_conn()
    skill_id = f"test:{uuid.uuid4().hex[:10]}"
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO skills "
                "(id, name, agent_role, version, status, input_schema, steps, output_contract, promoted_by, created_at) "
                "VALUES (%s,%s,'sre',1,'active','{}', %s,'{}','test',NOW())",
                (skill_id, skill_id, json.dumps([{"action": "observability_query", "on_failure": "ABORT"}])),
            )
        conn.commit()

    client = _operator_client()
    result = await client.call_tool("registry_execute_skill", {"skill_id": skill_id, "inputs": {}})
    assert result["skill_id"] == skill_id
    assert result["steps_completed"] >= 1


# ---------------------------------------------------------------------------
# registry__label_episode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_label_episode():
    episode_id = _insert_episode("sre")

    client = _sre_client()
    result = await client.call_tool(
        "registry_label_episode",
        {"episode_id": episode_id, "outcome": "RESOLVED", "outcome_signal": {"source": "test"}},
    )
    assert result.get("labeled") is True or "episode_id" in result


# ---------------------------------------------------------------------------
# registry__promote_candidate — operator only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_promote_candidate_requires_operator():
    client = _sre_client()
    with pytest.raises(ToolAccessDenied):
        await client.call_tool("registry_promote_candidate", {"candidate_id": "fake-id"})
