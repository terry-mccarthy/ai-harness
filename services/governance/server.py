import asyncio
import base64
import hashlib
import json
import logging
import os
import time

import httpx
import jwt
import pymysql
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from fastapi import BackgroundTasks, FastAPI, HTTPException, Header, Request, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

app = FastAPI()

# ---------------------------------------------------------------------------
# RSA keypair — load from file (preferred) or inline PEM env var
# ---------------------------------------------------------------------------

_TEST_KEY_FINGERPRINT = "sha256:f51572658f267e254a18caf6d2320581aacfbaee028a2a875a8a47af4630ffb5"

_key_file = os.environ.get("JWT_PRIVATE_KEY_FILE")
if _key_file:
    with open(_key_file, "rb") as _f:
        _jwt_private_key_pem = _f.read()
else:
    _jwt_private_key_pem = os.environ["JWT_PRIVATE_KEY"].encode()

_key_fingerprint = "sha256:" + hashlib.sha256(_jwt_private_key_pem).hexdigest()
if _key_fingerprint == _TEST_KEY_FINGERPRINT and os.environ.get("ENV") != "test":
    raise RuntimeError(
        "Governance is configured with the committed test key. "
        "Set ENV=test or supply a production key via JWT_PRIVATE_KEY_FILE."
    )

_private_key = load_pem_private_key(_jwt_private_key_pem, password=None)
_public_key = _private_key.public_key()


def _b64url(n: int) -> str:
    byte_len = (n.bit_length() + 7) // 8
    return base64.urlsafe_b64encode(n.to_bytes(byte_len, "big")).rstrip(b"=").decode()


OPA_URL = os.environ.get("OPA_URL", "http://opa:8181")
DOLT_HOST = os.environ.get("DOLT_HOST", "dolt")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))
DOLT_USER = os.environ.get("DOLT_USER", "harness")
DOLT_PASSWORD = os.environ.get("DOLT_PASSWORD", "harness")
DOLT_DB = os.environ.get("DOLT_DB", "harness")
TOKEN_TTL = int(os.environ.get("TOKEN_TTL", "900"))  # 15 min

# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------
_tool_calls_total = Counter(
    "harness_tool_calls_total",
    "Total tool invocations by agent role and decision",
    ["agent_role", "decision"],
)
_tool_call_latency = Histogram(
    "harness_tool_call_latency_ms",
    "Tool call latency in milliseconds",
    ["agent_role"],
    buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000, 10000],
)

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
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing_token")
    raw_token = authorization[7:]
    try:
        return jwt.decode(raw_token, _public_key, algorithms=["RS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token_expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "invalid_token")


async def _check_opa(role: str, short_tool: str) -> bool:
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


def get_dolt_conn():
    return pymysql.connect(
        host=DOLT_HOST,
        port=DOLT_PORT,
        user=DOLT_USER,
        password=DOLT_PASSWORD,
        database=DOLT_DB,
        autocommit=True,
    )


def _write_audit(agent_id, tool_name, server_id, req_hash, resp_hash, decision, rule, latency_ms):
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


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


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
    access_token = jwt.encode(payload, _private_key, algorithm="RS256")
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": TOKEN_TTL,
    }


# ---------------------------------------------------------------------------
# Policy check — replaces the old proxy endpoint
# ---------------------------------------------------------------------------


@app.post("/check")
async def check_policy(
    request: Request,
    authorization: str | None = Header(default=None),
    x_human_approval_token: str | None = Header(default=None),
):
    """Validate token and OPA policy. Returns {allowed, role, agent_id, rule}."""
    claims = _decode_jwt(authorization)
    body = await request.json()
    full_tool = body.get("tool_name", "")
    short_tool = full_tool.split("__")[-1] if "__" in full_tool else full_tool
    rule = f"harness.allow[{claims['role']}]"

    if short_tool == "shell_exec" and not x_human_approval_token:
        raise HTTPException(403, "shell_exec_requires_human_approval")

    if not await _check_opa(claims["role"], short_tool):
        _tool_calls_total.labels(agent_role=claims["role"], decision="deny").inc()
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


@app.post("/audit", status_code=202)
async def audit(
    request: Request,
    background_tasks: BackgroundTasks,
    authorization: str | None = Header(default=None),
):
    """Accept an audit record from GatewayClient and write it to Dolt async."""
    claims = _decode_jwt(authorization)
    body = await request.json()
    full_tool = body.get("tool_name", "")
    short_tool = full_tool.split("__")[-1] if "__" in full_tool else full_tool
    rule = body.get("rule", f"harness.allow[{claims['role']}]")
    req_hash = body.get("req_hash", "")
    resp_hash = body.get("resp_hash", "")
    decision = body.get("decision", "allow")
    latency_ms = int(body.get("latency_ms", 0))

    _tool_calls_total.labels(agent_role=claims["role"], decision=decision).inc()
    if latency_ms:
        _tool_call_latency.labels(agent_role=claims["role"]).observe(latency_ms)

    background_tasks.add_task(
        _write_audit,
        claims["sub"],
        full_tool,
        short_tool,
        req_hash,
        resp_hash,
        decision,
        rule,
        latency_ms,
    )
    return {}


# ---------------------------------------------------------------------------
# JWKS — public key for downstream verifiers
# ---------------------------------------------------------------------------


@app.get("/jwks")
async def jwks():
    pub = _public_key.public_numbers()
    return {
        "keys": [{
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "1",
            "n": _b64url(pub.n),
            "e": _b64url(pub.e),
        }]
    }


# ---------------------------------------------------------------------------
# Metrics + security endpoints (unchanged)
# ---------------------------------------------------------------------------


@app.get("/metrics")
async def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/memory/write")
async def memory_write(
    request: Request,
    authorization: str | None = Header(default=None),
):
    """Auth-gated memory write proxy. Requires a valid Bearer token."""
    _decode_jwt(authorization)
    body = await request.json()
    logger.info(
        "memory_write: ns=%s key=%s by agent=%s",
        body.get("namespace"),
        body.get("key"),
        "authenticated",
    )
    return {"status": "ok"}
