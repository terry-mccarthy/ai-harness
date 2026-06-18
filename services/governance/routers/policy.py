"""Policy + identity endpoints: /oauth/token, /check, /audit, /jwks, /metrics."""
import logging
import time

import jwt
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from core.auth import decode_jwt
from core.config import (
    CLIENTS,
    EXPIRY_PASS_INTERVAL,
    PRIVATE_KEY,
    PUBLIC_KEY,
    TOKEN_TTL,
    b64url,
)
from core.dolt import write_audit, write_episode, write_gate_failure
from core.metrics import tool_call_latency, tool_calls_total
from core.opa import check_opa

logger = logging.getLogger(__name__)

router = APIRouter()

_audit_call_count: int = 0


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@router.post("/oauth/token")
async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")

    if grant_type != "client_credentials":
        raise HTTPException(400, "unsupported_grant_type")
    client = CLIENTS.get(client_id)
    if not client or client["secret"] != client_secret:
        raise HTTPException(401, "invalid_client")

    now = int(time.time())
    payload = {
        "sub": client_id,
        "role": client["role"],
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    access_token = jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": TOKEN_TTL,
    }


# ---------------------------------------------------------------------------
# Policy check — replaces the old proxy endpoint
# ---------------------------------------------------------------------------


@router.post("/check")
async def check_policy(
    request: Request,
    authorization: str | None = Header(default=None),
    x_human_approval_token: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """Validate token and OPA policy. Returns {allowed, role, agent_id, rule}."""
    claims = decode_jwt(authorization)
    body = await request.json()
    full_tool = body.get("tool_name", "")
    short_tool = full_tool.split("__")[-1] if "__" in full_tool else full_tool
    rule = f"harness.allow[{claims['role']}]"

    if short_tool == "shell_exec" and not x_human_approval_token:
        tool_calls_total.labels(agent_role=claims["role"], decision="deny").inc()
        write_audit(
            claims["sub"], full_tool, short_tool, "", "", "deny",
            "shell_exec_requires_human_approval", 0, x_correlation_id,
        )
        raise HTTPException(403, "shell_exec_requires_human_approval")

    if not await check_opa("harness/allow", {"agent_role": claims["role"], "tool_name": short_tool}):
        tool_calls_total.labels(agent_role=claims["role"], decision="deny").inc()
        write_audit(
            claims["sub"], full_tool, short_tool, "", "", "deny",
            rule, 0, x_correlation_id,
        )
        raise HTTPException(403, "policy_denied")

    return {
        "allowed": True,
        "role": claims["role"],
        "agent_id": claims["sub"],
        "rule": rule,
    }


# ---------------------------------------------------------------------------
# Audit — async Dolt write called by GatewayClient post-invocation
# ---------------------------------------------------------------------------


@router.post("/audit", status_code=202)
async def audit(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
    x_correlation_id: str | None = Header(default=None),
):
    """Accept an audit record from GatewayClient and write it to Dolt async."""
    global _audit_call_count
    claims = decode_jwt(authorization)
    body = await request.json()
    full_tool = body.get("tool_name", "")
    short_tool = full_tool.split("__")[-1] if "__" in full_tool else full_tool
    rule = body.get("rule", f"harness.allow[{claims['role']}]")
    req_hash = body.get("req_hash", "")
    resp_hash = body.get("resp_hash", "")
    decision = body.get("decision", "allow")
    latency_ms = int(body.get("latency_ms", 0))
    correlation_id = x_correlation_id or body.get("correlation_id")

    tool_calls_total.labels(agent_role=claims["role"], decision=decision).inc()
    if latency_ms:
        tool_call_latency.labels(agent_role=claims["role"]).observe(latency_ms)

    background_tasks.add_task(
        write_audit,
        claims["sub"],
        full_tool,
        short_tool,
        req_hash,
        resp_hash,
        decision,
        rule,
        latency_ms,
        correlation_id,
    )
    background_tasks.add_task(
        write_episode,
        claims["sub"],
        full_tool,
        short_tool,
        req_hash,
        correlation_id,
        body.get("service_class"),
    )
    _audit_call_count += 1
    if EXPIRY_PASS_INTERVAL > 0 and _audit_call_count % EXPIRY_PASS_INTERVAL == 0:
        from routers.skills import background_expiry_pass  # lazy: lives in slice 04
        background_tasks.add_task(background_expiry_pass)
    return {}


# ---------------------------------------------------------------------------
# Architectural gate failure audit
# ---------------------------------------------------------------------------


@router.post("/audit/architectural-gate", status_code=202)
async def architectural_gate_audit(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    """Record an architectural gate failure in Dolt (async)."""
    claims = decode_jwt(authorization)
    body = await request.json()
    background_tasks.add_task(
        write_gate_failure,
        thread_id=body.get("thread_id"),
        rule=body.get("rule"),
        severity=body.get("severity"),
        file=body.get("file"),
        message=body.get("message"),
        task=body.get("task"),
        repo_path=body.get("repo_path"),
        target_language=body.get("target_language"),
        gate_signal=body.get("gate_signal"),
    )
    tool_calls_total.labels(agent_role=claims["role"], decision="deny").inc()
    return {}


# ---------------------------------------------------------------------------
# JWKS — public key for downstream verifiers
# ---------------------------------------------------------------------------


@router.get("/jwks")
async def jwks():
    pub = PUBLIC_KEY.public_numbers()
    return {
        "keys": [{
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "1",
            "n": b64url(pub.n),
            "e": b64url(pub.e),
        }]
    }


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


@router.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
