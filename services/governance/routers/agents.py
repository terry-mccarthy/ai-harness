"""Agent orchestration endpoints: /agent/invoke and /agents."""
import json
import logging
import os
import time

import httpx
import jwt
from fastapi import APIRouter, BackgroundTasks, HTTPException, Header, Request

from core.auth import decode_jwt
from core.config import PRIVATE_KEY, TOKEN_TTL
from core.dolt import write_audit
from core.opa import check_opa

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Agent registry and discovery
# ---------------------------------------------------------------------------

MCPJUNGLE_URL = os.environ.get("MCPJUNGLE_URL", "http://mcpjungle:8080")

# Agent registry: known agents with their credentials, entry tools, and input schemas
_AGENT_REGISTRY: dict[str, dict] = {
    "code-reviewer": {
        "client_id": "code-reviewer",
        "secret_env": "CODE_REVIEWER_SECRET",
        "role": "code_reviewer",
        "entry_tool": "review_server__review_diff",
        "input_schema": {
            "type": "object",
            "required": ["repo"],
            "properties": {
                "repo": {"type": "string"},
                "base_ref": {"type": "string"},
                "head_ref": {"type": "string"},
                "diff_text": {"type": "string"},
            },
        },
    },
    "architect": {
        "client_id": "architect",
        "secret_env": "ARCHITECT_SECRET",
        "role": "architect",
        "entry_tool": "architect_stub__codebase_search",
        "input_schema": {
            "type": "object",
            "required": [],
            "properties": {
                "query": {"type": "string"},
                "decision": {"type": "string"},
            },
        },
    },
    "sre": {
        "client_id": "sre",
        "secret_env": "SRE_SECRET",
        "role": "sre",
        "entry_tool": "sre_stub__observability_query",
        "input_schema": {
            "type": "object",
            "required": [],
            "properties": {
                "query": {"type": "string"},
                "alert": {"type": "string"},
            },
        },
    },
}

_KNOWN_AGENTS = list(_AGENT_REGISTRY.keys())


def _validate_payload(schema: dict, payload: dict) -> list[str]:
    """Return list of validation errors, or empty list if valid."""
    errors = []
    required = schema.get("required", [])
    for field in required:
        if field not in payload:
            errors.append(f"missing required field: {field}")
    return errors


async def _call_mcpjungle(tool_name: str, params: dict) -> dict:
    body = {"name": tool_name, **params}
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{MCPJUNGLE_URL}/api/v0/tools/invoke",
            json=body,
            timeout=60.0,
        )
    data = resp.json()
    # Unwrap MCPJungle content wrapper
    content = data.get("content", [])
    if content and isinstance(content, list) and content[0].get("type") == "text":
        try:
            return json.loads(content[0]["text"])
        except (json.JSONDecodeError, KeyError):
            return {"text": content[0].get("text", "")}
    return data


@router.post("/agent/invoke")
async def agent_invoke(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """Synchronous governed handoff: validate OPA, mint target creds, forward."""
    claims = decode_jwt(authorization)
    caller_role = claims["role"]
    body = await request.json()
    correlation_id = x_correlation_id
    target = body.get("target", "")
    artifact_type = body.get("artifact_type", "")
    payload = body.get("payload", {})

    # Unknown target
    if target not in _AGENT_REGISTRY:
        raise HTTPException(404, f"unknown target: {target}")

    agent_spec = _AGENT_REGISTRY[target]

    # Payload schema validation (before any OPA/network call)
    errors = _validate_payload(agent_spec["input_schema"], payload)
    if errors:
        raise HTTPException(422, {"errors": errors})

    # OPA invoke check
    allowed_targets = await check_opa(
        "harness/invoke_allowed",
        {"role": caller_role, "action": "invoke", "target": target},
    )
    if target not in (allowed_targets or []):
        # Denied — write audit row synchronously (HTTPException cancels background tasks)
        write_audit(
            claims["sub"],
            f"agent_invoke:{target}",
            target,
            "",
            "",
            "deny",
            f"invoke_denied[{caller_role}->{target}]",
            0,
            correlation_id,
        )
        raise HTTPException(403, "invoke_denied_by_policy")

    # Mint target's own token (do NOT forward caller's token)
    secret = os.environ.get(agent_spec["secret_env"], f"{agent_spec['client_id']}-secret")
    now = int(time.time())
    target_token_payload = {
        "sub": agent_spec["client_id"],
        "role": agent_spec["role"],
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    target_token = jwt.encode(target_token_payload, PRIVATE_KEY, algorithm="RS256")

    # Call MCPJungle entry tool using target's identity
    result = await _call_mcpjungle(agent_spec["entry_tool"], payload)

    # Write audit as the target agent
    background_tasks.add_task(
        write_audit,
        agent_spec["client_id"],
        f"agent_invoke:{agent_spec['entry_tool']}",
        agent_spec["entry_tool"],
        "",
        "",
        "allow",
        f"invoke_allowed[{caller_role}->{target}]",
        0,
        correlation_id,
    )

    return result


@router.get("/agents")
async def agent_list(authorization: str | None = Header(default=None)):
    """Return the list of agents the calling role is permitted to invoke."""
    claims = decode_jwt(authorization)
    role = claims["role"]
    permitted = []
    for name in _KNOWN_AGENTS:
        allowed = await check_opa(
            "harness/invoke_allowed",
            {"role": role, "action": "invoke", "target": name},
        )
        if name in (allowed or []):
            permitted.append({"name": name})
    return permitted
