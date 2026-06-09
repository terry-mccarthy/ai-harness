import os
import time
import json
import hashlib
import logging
from fastapi import FastAPI, HTTPException, Header, Request
import httpx
import jwt
import pymysql

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

app = FastAPI()

JWT_SECRET = os.environ["JWT_SECRET"]
MCPJUNGLE_URL = os.environ["MCPJUNGLE_INTERNAL_URL"]
OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
DOLT_HOST = os.environ.get("DOLT_HOST", "dolt")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))
DOLT_USER = os.environ.get("DOLT_USER", "harness")
DOLT_PASSWORD = os.environ.get("DOLT_PASSWORD", "harness")
DOLT_DB = os.environ.get("DOLT_DB", "harness")
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "900"))  # 15 min
UPSTREAM_TIMEOUT = float(os.environ.get("UPSTREAM_TIMEOUT", "180"))

CLIENTS = {
    "architect": {
        "secret": os.environ.get("ARCHITECT_SECRET", "architect-secret"),
        "role": "architect",
    },
    "code-reviewer": {
        "secret": os.environ["CODE_REVIEWER_SECRET"],
        "role": "code_reviewer",
    },
    "sre": {
        "secret": os.environ.get("SRE_SECRET", "sre-secret"),
        "role": "sre",
    },
}


def _decode_jwt(authorization: str | None) -> dict:
    """Raises HTTPException on missing/expired/invalid token."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing_token")
    raw_token = authorization[7:]
    try:
        return jwt.decode(raw_token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "invalid_token")


async def _check_policy(role: str, short_tool: str) -> bool:
    """Returns True if OPA allows the call; False on deny or OPA error."""
    try:
        async with httpx.AsyncClient() as client:
            opa_resp = await client.post(
                f"{OPA_URL}/v1/data/harness/allow",
                json={"input": {"agent_role": role, "tool_name": short_tool}},
                timeout=5.0,
            )
        return opa_resp.json().get("result", False)
    except Exception as e:
        logger.error("OPA unreachable: %s", e)
        return False


async def _forward_to_mcpjungle(body: dict) -> tuple:
    """POST to MCPJungle; returns (response, latency_ms)."""
    start = int(time.time() * 1000)
    async with httpx.AsyncClient() as client:
        upstream = await client.post(
            f"{MCPJUNGLE_URL}/api/v0/tools/invoke",
            json=body,
            timeout=UPSTREAM_TIMEOUT,
        )
    return upstream, int(time.time() * 1000) - start


def get_dolt_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user=DOLT_USER,
        password=DOLT_PASSWORD,
        database=DOLT_DB,
        autocommit=True,
    )


@app.post("/oauth/token")
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
    access_token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": TOKEN_TTL,
    }


@app.post("/api/v0/tools/invoke")
async def invoke(
    request: Request,
    authorization: str | None = Header(default=None),
    x_human_approval_token: str | None = Header(default=None),
):
    claims = _decode_jwt(authorization)
    body = await request.json()
    full_tool = body.get("name", "")
    short_tool = full_tool.split("__")[-1] if "__" in full_tool else full_tool
    rule = f"harness.allow[{claims['role']}]"

    if short_tool == "shell_exec" and not x_human_approval_token:
        raise HTTPException(403, "shell_exec_requires_human_approval")

    if not await _check_policy(claims["role"], short_tool):
        _write_audit(claims["sub"], full_tool, short_tool, json.dumps(body), None, "deny", rule, 0)
        raise HTTPException(403, "policy_denied")

    upstream, latency = await _forward_to_mcpjungle(body)
    req_hash = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    resp_hash = hashlib.sha256(upstream.text.encode()).hexdigest()[:16]
    _write_audit(claims["sub"], full_tool, short_tool, req_hash, resp_hash, "allow", rule, latency)

    upstream.raise_for_status()
    return upstream.json()


def _write_audit(
    agent_id,
    tool_name,
    server_id,
    req_hash,
    resp_hash,
    decision,
    rule,
    latency_ms,
):
    conn = None
    try:
        conn = get_dolt_conn()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO audit_log
                   (agent_id, tool_name, server_id, request_hash, response_hash,
                    policy_decision, policy_rule, timestamp_ms, latency_ms)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    agent_id,
                    tool_name,
                    server_id,
                    req_hash,
                    resp_hash,
                    decision,
                    rule,
                    int(time.time() * 1000),
                    latency_ms,
                ),
            )
            cur.execute(
                "CALL DOLT_COMMIT('-Am', %s)",
                (f"audit: {tool_name} by {agent_id} [{decision}]",),
            )
    except Exception as e:
        logger.error("Dolt audit write failed: %s", e)
    finally:
        if conn:
            conn.close()
