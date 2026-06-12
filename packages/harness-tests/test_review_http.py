"""Unit tests for the review server's plain HTTP POST /review endpoint.

No Docker stack needed — the FastMCP app is exercised via httpx's ASGI
transport, with GatewayClient and LLMProvider mocked in-process.
"""
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "review_server"))

pytestmark = pytest.mark.asyncio

_VALID_REVIEW = json.dumps({
    "verdict": "pass",
    "findings": [],
    "summary": "Looks good.",
})

_SAMPLE_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x=1\n+x=2"


@asynccontextmanager
async def _review_client(llm_response: str = _VALID_REVIEW):
    """Yield an httpx AsyncClient wired to the review server ASGI app.

    GatewayClient and LLMProvider are replaced with in-process mocks for the
    duration of the context, so no Docker stack is required.
    """
    import server as review_server

    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    class _MockLLM:
        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            return LLMResponse(content=llm_response)

    app = review_server.mcp.streamable_http_app()

    with (
        patch.object(review_server, "_build_llm_provider", return_value=_MockLLM()),
        patch("server.GatewayClient", return_value=mock_gateway),
        patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


# ---------------------------------------------------------------------------
# Slice 1 — endpoint is reachable
# ---------------------------------------------------------------------------

async def test_http_review_endpoint_exists():
    """`POST /review` returns 200 for a valid diff."""
    async with _review_client() as client:
        resp = await client.post("/review", json={"diff_text": _SAMPLE_DIFF})
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Slice 2 — response shape
# ---------------------------------------------------------------------------

async def test_http_review_returns_verdict_and_findings():
    async with _review_client() as client:
        resp = await client.post("/review", json={"diff_text": _SAMPLE_DIFF})
    body = resp.json()
    assert "verdict" in body
    assert "findings" in body
    assert "summary" in body


async def test_http_review_verdict_pass_on_clean_diff():
    async with _review_client(_VALID_REVIEW) as client:
        resp = await client.post("/review", json={"diff_text": _SAMPLE_DIFF})
    assert resp.json()["verdict"] == "pass"


# ---------------------------------------------------------------------------
# Slice 3 — optional fields
# ---------------------------------------------------------------------------

async def test_http_review_accepts_custom_task():
    async with _review_client() as client:
        resp = await client.post("/review", json={
            "diff_text": _SAMPLE_DIFF,
            "task": "Only check for SQL injection.",
        })
    assert resp.status_code == 200


async def test_http_review_accepts_provider_override():
    async with _review_client() as client:
        resp = await client.post("/review", json={
            "diff_text": _SAMPLE_DIFF,
            "provider": "ollama",
        })
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Slice 4 — error handling
# ---------------------------------------------------------------------------

async def test_http_review_missing_diff_text_returns_422():
    async with _review_client() as client:
        resp = await client.post("/review", json={})
    assert resp.status_code == 422


async def test_http_review_agent_error_returns_500():
    """When the agent can't produce valid JSON after all retries, return 500."""
    import server as review_server

    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    class _ErrorLLM:
        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            return LLMResponse(content="not-json")  # always fails schema validation

    app = review_server.mcp.streamable_http_app()

    with (
        patch.object(review_server, "_build_llm_provider", return_value=_ErrorLLM()),
        patch("server.GatewayClient", return_value=mock_gateway),
        patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/review", json={"diff_text": _SAMPLE_DIFF})

    assert resp.status_code == 500
