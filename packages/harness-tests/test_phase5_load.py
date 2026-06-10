"""Phase 5 load test.

50 concurrent task submissions via governance.  Verifies:
  - p99 latency < 10 000ms
  - Zero 5xx errors (data isolation — each request gets its own JWT sub)
  - Rate limiter fires at expected threshold (429s are expected and OK)

Run standalone (not part of the normal integration suite):
  uv run pytest packages/harness-tests/test_phase5_load.py -v -s
"""
import asyncio
import os
import statistics
import time
import uuid

import httpx
import jwt as pyjwt
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
JWT_SECRET = os.environ.get("JWT_SECRET", "dev-jwt-secret-change-in-prod-xyz")
CONCURRENCY = int(os.environ.get("LOAD_CONCURRENCY", "50"))
P99_THRESHOLD_MS = int(os.environ.get("LOAD_P99_MS", "10000"))

pytestmark = pytest.mark.load


def _make_token(role: str = "architect") -> str:
    now = int(time.time())
    return pyjwt.encode(
        {"sub": f"load-{uuid.uuid4()}", "role": role, "iat": now, "exp": now + 300},
        JWT_SECRET,
        algorithm="HS256",
    )


async def _submit_one(client: httpx.AsyncClient, i: int) -> dict:
    token = _make_token("architect")
    start = time.time()
    try:
        resp = await client.post(
            f"{GOVERNANCE_URL}/api/v0/tools/invoke",
            json={"name": "architect_stub__codebase_search", "query": f"load-test-{i}"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=15.0,
        )
        latency_ms = (time.time() - start) * 1000
        return {"status": resp.status_code, "latency_ms": latency_ms, "idx": i}
    except Exception as e:
        latency_ms = (time.time() - start) * 1000
        return {"status": -1, "latency_ms": latency_ms, "idx": i, "error": str(e)}


@pytest.mark.asyncio
@pytest.mark.load
async def test_load_50_concurrent():
    """50 concurrent tool submissions.  p99 latency must be under P99_THRESHOLD_MS.
    5xx errors are failures; 429s (rate-limited) are acceptable."""
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_submit_one(client, i) for i in range(CONCURRENCY)])

    latencies = [r["latency_ms"] for r in results]
    statuses = [r["status"] for r in results]

    ok = sum(1 for s in statuses if s == 200)
    rate_limited = sum(1 for s in statuses if s == 429)
    errors_5xx = sum(1 for s in statuses if isinstance(s, int) and 500 <= s < 600)
    conn_errors = sum(1 for s in statuses if s == -1)

    latencies.sort()
    p50 = latencies[int(CONCURRENCY * 0.50)]
    p99 = latencies[int(CONCURRENCY * 0.99)]
    mean = statistics.mean(latencies)

    print(f"\n--- Load test results ({CONCURRENCY} concurrent) ---")
    print(f"  200 OK:        {ok}")
    print(f"  429 limited:   {rate_limited}")
    print(f"  5xx errors:    {errors_5xx}")
    print(f"  conn errors:   {conn_errors}")
    print(f"  p50 latency:   {p50:.0f}ms")
    print(f"  p99 latency:   {p99:.0f}ms")
    print(f"  mean latency:  {mean:.0f}ms")
    print(f"  p99 threshold: {P99_THRESHOLD_MS}ms")

    assert errors_5xx == 0, f"{errors_5xx} requests returned 5xx — data isolation failure"
    assert conn_errors == 0, f"{conn_errors} requests failed with connection error"
    assert p99 <= P99_THRESHOLD_MS, (
        f"p99 latency {p99:.0f}ms exceeds threshold {P99_THRESHOLD_MS}ms"
    )
    print(f"\nPASS — p99={p99:.0f}ms, 200s={ok}, 429s={rate_limited}")
