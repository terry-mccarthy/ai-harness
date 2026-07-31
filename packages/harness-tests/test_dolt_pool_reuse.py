"""Smoke test for issue #03 — pooled async Dolt connections.

Before this issue, write_audit/write_episode/write_gate_failure each opened a
brand-new pymysql connection per call (via get_dolt_conn()) and closed it in a
`finally` block — sync code blocking the event loop, with per-call TCP/auth
churn under load. This test proves the replacement (a shared aiomysql pool,
created once and reused) actually behaves that way against a live Dolt: after
firing many concurrent /audit requests, the number of live connections stays
bounded near the pool's configured maxsize instead of growing with request
count.
"""
import os

import httpx
import pymysql
import pymysql.cursors
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
DOLT_HOST = os.environ.get("DOLT_HOST", "localhost")
DOLT_PORT = int(os.environ.get("DOLT_PORT", "3306"))
# SHOW PROCESSLIST needs elevated privilege; the `harness` app user (used by
# other tests for read-only verification queries) doesn't have it. Governance
# itself connects to Dolt as DOLT_USER/DOLT_PASSWORD (root/root in
# docker-compose.yml), so reuse those credentials here too.
DOLT_ADMIN_USER = os.environ.get("DOLT_USER", "root")
DOLT_ADMIN_PASSWORD = os.environ.get("DOLT_PASSWORD", "root")

CONCURRENCY = 30
# init_pool() in services/governance/core/dolt.py sets maxsize=10. Allow a
# little headroom for the harness's own verification connection plus any
# stray connections from other governance routes, without allowing the
# per-call-connection behavior this issue removed (which would show up as
# connection counts scaling with CONCURRENCY).
MAX_EXPECTED_CONNECTIONS = 15


def _get_token() -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={"grant_type": "client_credentials", "client_id": "sre", "client_secret": os.environ.get("SRE_SECRET", "sre-secret")},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _connection_count() -> int:
    conn = pymysql.connect(
        host=DOLT_HOST, port=DOLT_PORT,
        user=DOLT_ADMIN_USER, password=DOLT_ADMIN_PASSWORD,
        database="harness", connect_timeout=5, autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )
    with conn:
        with conn.cursor() as cur:
            cur.execute("SHOW PROCESSLIST")
            return len(cur.fetchall())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_audits_reuse_pooled_connections():
    """CONCURRENCY concurrent /audit calls must not create CONCURRENCY new
    Dolt connections — the shared pool should cap connection count near its
    configured maxsize regardless of request volume."""
    import asyncio

    token = _get_token()

    async def _one(client: httpx.AsyncClient, i: int) -> int:
        resp = await client.post(
            f"{GOVERNANCE_URL}/audit",
            json={"tool_name": "sre_stub__observability_query", "decision": "allow", "latency_ms": 1},
            headers={"Authorization": f"Bearer {token}", "X-Correlation-Id": f"pool-reuse-{i}"},
            timeout=15.0,
        )
        return resp.status_code

    async with httpx.AsyncClient() as client:
        statuses = await asyncio.gather(*[_one(client, i) for i in range(CONCURRENCY)])

    assert all(s == 202 for s in statuses), f"unexpected statuses: {statuses}"

    # 0.5s isn't always enough: governance's EXPIRY_PASS_INTERVAL is set low
    # for dev/test (docker-compose.yml), so a burst of CONCURRENCY /audit
    # calls can trigger a few background_expiry_pass() runs. Those use their
    # own unpooled sync connection (get_dolt_conn(), deliberately out of
    # scope for this issue's pooling — see its docstring) and can briefly
    # push the live count above the pool's steady-state size before settling
    # back down once each pass finishes.
    await asyncio.sleep(2)
    live_connections = _connection_count()
    assert live_connections <= MAX_EXPECTED_CONNECTIONS, (
        f"expected pooled connection count <= {MAX_EXPECTED_CONNECTIONS} after "
        f"{CONCURRENCY} concurrent writes, got {live_connections} — looks like "
        "per-call connections are being opened instead of reusing the pool"
    )
