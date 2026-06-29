"""Skills Registry MCP server — exposes governance skill lifecycle as MCP tools.

All governance calls use the human-operator OAuth client credentials
(REGISTRY_OPERATOR_SECRET env var). OPA at governance enforces scope.
"""
import json
import logging
import os
import time

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from harness_gateway.client import GatewayClient
from harness_gateway.skill_runner import SkillRunner

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090").rstrip("/")

_ENV = os.environ.get("ENV", "production")
_DEV_DEFAULTS = {
    "REGISTRY_OPERATOR_SECRET": "human-operator-secret",
    "SRE_SECRET": "sre-secret",
    "CODE_REVIEWER_SECRET": "code-reviewer-secret",
    "ARCHITECT_SECRET": "architect-secret",
}


def _get_secret(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        if _ENV == "test":
            return _DEV_DEFAULTS[name]
        raise RuntimeError(f"{name} must be set in non-test environments")
    if _ENV != "test" and val == _DEV_DEFAULTS.get(name):
        logger.warning("SECURITY: %s is using the default dev value — set a strong secret in production", name)
    return val


REGISTRY_OPERATOR_SECRET = _get_secret("REGISTRY_OPERATOR_SECRET")

_ROLE_CREDENTIALS: dict[str, tuple[str, str]] = {
    "sre": ("sre", _get_secret("SRE_SECRET")),
    "code_reviewer": ("code-reviewer", _get_secret("CODE_REVIEWER_SECRET")),
    "architect": ("architect", _get_secret("ARCHITECT_SECRET")),
    "human_operator": ("human-operator", REGISTRY_OPERATOR_SECRET),
}

mcp = FastMCP(
    "registry",
    host="0.0.0.0",
    port=9006,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_token_cache: dict = {}


def _get_operator_token() -> str:
    cached = _token_cache.get("token")
    expires_at = _token_cache.get("expires_at", 0)
    if cached and time.time() < expires_at - 30:
        return cached
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "human-operator",
            "client_secret": REGISTRY_OPERATOR_SECRET,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = time.time() + data.get("expires_in", 900)
    return data["access_token"]


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {_get_operator_token()}"}


def _gov_get(path: str, **params) -> dict | list:
    resp = httpx.get(f"{GOVERNANCE_URL}{path}", headers=_auth_headers(), params=params or None, timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _gov_post(path: str, body: dict | None = None, expected_status: int = 200) -> dict:
    resp = httpx.post(
        f"{GOVERNANCE_URL}{path}",
        headers=_auth_headers(),
        json=body or {},
        timeout=15.0,
    )
    if resp.status_code != expected_status and resp.status_code not in (200, 201):
        resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_skills(status_filter: str = "active") -> dict:
    """List skills from the registry.

    Args:
        status_filter: One of "active", "expired", "revoked". Defaults to "active".
    """
    return {"skills": _gov_get("/skills", status=status_filter)}


@mcp.tool()
def get_skill(skill_id: str) -> dict:
    """Get full detail for a single skill.

    Args:
        skill_id: The skill UUID or slug.
    """
    return _gov_get(f"/skills/{skill_id}")


@mcp.tool()
def get_skill_prompt(skill_id: str) -> dict:
    """Get the prompt_template for a skill.

    Returns 410 if the skill is revoked or expired.

    Args:
        skill_id: The skill UUID or slug.
    """
    return _gov_get(f"/skills/{skill_id}/prompt")


@mcp.tool()
def list_episodes(limit: int = 20, unlabeled_only: bool = False) -> dict:
    """List recent episodes from the registry.

    Args:
        limit: Max episodes to return (default 20).
        unlabeled_only: If true, return only unlabeled episodes.
    """
    return {"episodes": _gov_get("/episodes", limit=limit, unlabeled=str(unlabeled_only).lower())}


@mcp.tool()
def get_episode(episode_id: str) -> dict:
    """Get a single episode by ID.

    Args:
        episode_id: The episode UUID.
    """
    return _gov_get(f"/episodes/{episode_id}")


@mcp.tool()
def list_candidates(status_filter: str | None = None) -> dict:
    """List skill candidates.

    Args:
        status_filter: Optional status filter (e.g. "PROPOSED", "PROMOTED").
    """
    params = {}
    if status_filter:
        params["status"] = status_filter
    return {"candidates": _gov_get("/candidates", **params)}


@mcp.tool()
def get_candidate(candidate_id: str) -> dict:
    """Get a single candidate by ID.

    Args:
        candidate_id: The candidate UUID.
    """
    return _gov_get(f"/candidates/{candidate_id}")


# ---------------------------------------------------------------------------
# Episode labeling and candidate proposal (sre / code_reviewer scope)
# ---------------------------------------------------------------------------


@mcp.tool()
def label_episode(episode_id: str, outcome: str, outcome_signal: dict | None = None) -> dict:
    """Label the outcome of an episode.

    Args:
        episode_id: The episode UUID to label.
        outcome: "RESOLVED", "FAILED", "ROLLED_BACK", "HUMAN_OVERRIDE", or "INCONCLUSIVE".
        outcome_signal: Optional dict with additional signal metadata.
    """
    body: dict = {"outcome": outcome}
    if outcome_signal:
        body["outcome_signal"] = outcome_signal
    return _gov_post(f"/episodes/{episode_id}/label", body)


@mcp.tool()
def propose_candidate(episode_ids: list[str]) -> dict:
    """Propose a skill candidate from a set of qualified labeled episodes.

    Args:
        episode_ids: List of episode UUIDs to base the candidate on.
    """
    return _gov_post("/candidates", {"episode_ids": episode_ids}, expected_status=201)


# ---------------------------------------------------------------------------
# Human-operator write tools
# ---------------------------------------------------------------------------


@mcp.tool()
def create_skill(
    skill_name: str,
    agent_role: str,
    description: str,
    prompt_template: str,
    steps: list[dict],
    preconditions: dict | None = None,
    input_schema: dict | None = None,
    output_contract: dict | None = None,
) -> dict:
    """Manually author and register a new skill in the registry.

    Args:
        skill_name: Human-readable name (e.g. "triage-db-latency").
        agent_role: One of "sre", "code_reviewer", "architect".
        description: One-sentence description for TF-IDF lookup.
        prompt_template: Full system prompt / instruction block.
        steps: Ordered list of tool calls (action, params, on_failure).
        preconditions: Optional env_constraints and task_patterns.
        input_schema: Optional JSON Schema for inputs.
        output_contract: Optional JSON Schema for expected output.
    """
    body = {
        "name": skill_name,
        "agent_role": agent_role,
        "description": description,
        "prompt_template": prompt_template,
        "steps": steps,
        "preconditions": preconditions or {},
        "input_schema": input_schema or {},
        "output_contract": output_contract or {},
    }
    return _gov_post("/skills/author", body, expected_status=201)


@mcp.tool()
def promote_candidate(candidate_id: str) -> dict:
    """Promote a proposed candidate to an active skill. Human-operator only.

    Args:
        candidate_id: The candidate UUID to promote.
    """
    return _gov_post(f"/candidates/{candidate_id}/promote")


@mcp.tool()
def reject_candidate(candidate_id: str, reason: str) -> dict:
    """Reject a proposed candidate. Human-operator only.

    Args:
        candidate_id: The candidate UUID to reject.
        reason: Required rejection reason.
    """
    return _gov_post(f"/candidates/{candidate_id}/reject", {"reason": reason})


@mcp.tool()
def revoke_skill(skill_id: str, reason: str) -> dict:
    """Revoke an active skill immediately. Human-operator only.

    Args:
        skill_id: The skill UUID or slug to revoke.
        reason: Required revocation reason.
    """
    return _gov_post(f"/skills/{skill_id}/revoke", {"reason": reason})


# ---------------------------------------------------------------------------
# Skill execution
# ---------------------------------------------------------------------------


@mcp.tool()
async def execute_skill(skill_id: str, inputs: dict | None = None) -> dict:
    """Execute a promoted skill step-by-step. OPA is re-checked on every step.

    Step execution uses the skill's agent_role credentials so OPA enforces
    the correct tool-access policy for that role.

    Args:
        skill_id: The skill UUID or slug to execute.
        inputs: Optional dict of inputs passed to each step.
    """
    # Fetch skill to determine agent_role for step-level OPA checks
    skill = _gov_get(f"/skills/{skill_id}")
    agent_role = skill.get("agent_role", "sre")
    client_id, secret = _ROLE_CREDENTIALS.get(agent_role, _ROLE_CREDENTIALS["sre"])

    mcpjungle_url = os.environ.get("MCPJUNGLE_URL", "http://mcpjungle:8080")
    gateway = GatewayClient(
        gateway_url=mcpjungle_url,
        governance_url=GOVERNANCE_URL,
        client_id=client_id,
        client_secret=secret,
    )
    runner = SkillRunner(gateway)
    return await runner.execute(skill_id, inputs or {})


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9006)
