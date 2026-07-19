"""Unit tests for the review server's POST /review-adversarial endpoint and the
adversarial_review MCP tool wiring. Mirrors test_review_http.py's approach:
GatewayClient and LLMProvider mocked in-process, no Docker stack needed.
"""
import importlib.util
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

_REVIEW_SERVER_PATH = Path(__file__).resolve().parents[2] / "services" / "review_server" / "server.py"
_REVIEW_SERVER_MODULE = "_review_server_under_test"


def _load_review_server():
    if _REVIEW_SERVER_MODULE in sys.modules:
        return sys.modules[_REVIEW_SERVER_MODULE]
    rs_dir = str(_REVIEW_SERVER_PATH.parent)
    if rs_dir not in sys.path:
        sys.path.insert(0, rs_dir)
    spec = importlib.util.spec_from_file_location(_REVIEW_SERVER_MODULE, _REVIEW_SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_REVIEW_SERVER_MODULE] = mod
    spec.loader.exec_module(mod)
    return mod


_VALID_CRITIC_OUTPUT = json.dumps({
    "findings": [
        {
            "outcome": "confirmed",
            "severity": "CRITICAL",
            "file": "x.py",
            "line": 1,
            "message": "sql injection",
            "exploit_scenario": "username=\"' OR '1'='1\" returns all rows",
        }
    ],
    "summary": "Confirmed with a working exploit.",
})

_SAMPLE_DIFF = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-x=1\n+x=2"
_FIRST_PASS_OUTPUT = {"verdict": "fail", "findings": [], "summary": "first pass"}


@asynccontextmanager
async def _adversarial_client(llm_response: str = _VALID_CRITIC_OUTPUT, api_key: str | None = None):
    review_server = _load_review_server()

    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    class _MockLLM:
        provider_name = "ollama"
        model_name = "test-model"

        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            return LLMResponse(content=llm_response)

    app = review_server.mcp.streamable_http_app()

    env = {"MCPJUNGLE_URL": "http://mock-jungle:8080"}
    if api_key is not None:
        env["REVIEW_API_KEY"] = api_key

    with (
        patch.object(review_server, "_build_llm_provider", return_value=_MockLLM()),
        patch.object(review_server, "GatewayClient", return_value=mock_gateway),
        patch.dict("os.environ", env, clear=False),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client


async def test_http_adversarial_review_endpoint_exists():
    async with _adversarial_client() as client:
        resp = await client.post("/review-adversarial", json={
            "diff_text": _SAMPLE_DIFF,
            "first_pass_output": _FIRST_PASS_OUTPUT,
        })
    assert resp.status_code == 200


async def test_http_adversarial_review_returns_findings_and_summary():
    async with _adversarial_client() as client:
        resp = await client.post("/review-adversarial", json={
            "diff_text": _SAMPLE_DIFF,
            "first_pass_output": _FIRST_PASS_OUTPUT,
        })
    body = resp.json()
    assert "findings" in body
    assert "summary" in body
    assert body["findings"][0]["outcome"] == "confirmed"
    assert body["findings"][0]["exploit_scenario"]


async def test_http_adversarial_review_missing_diff_text_returns_422():
    async with _adversarial_client() as client:
        resp = await client.post("/review-adversarial", json={"first_pass_output": _FIRST_PASS_OUTPUT})
    assert resp.status_code == 422


async def test_http_adversarial_review_missing_first_pass_output_returns_422():
    async with _adversarial_client() as client:
        resp = await client.post("/review-adversarial", json={"diff_text": _SAMPLE_DIFF})
    assert resp.status_code == 422


async def test_http_adversarial_review_agent_error_returns_400():
    review_server = _load_review_server()
    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    class _ErrorLLM:
        provider_name = "ollama"
        model_name = "test-model"

        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            return LLMResponse(content="not-json")

    app = review_server.mcp.streamable_http_app()
    with (
        patch.object(review_server, "_build_llm_provider", return_value=_ErrorLLM()),
        patch.object(review_server, "GatewayClient", return_value=mock_gateway),
        patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/review-adversarial", json={
                "diff_text": _SAMPLE_DIFF,
                "first_pass_output": _FIRST_PASS_OUTPUT,
            })

    assert resp.status_code == 400
    assert "max retries" in resp.json().get("error", "").lower()


async def test_http_adversarial_review_wrong_key_returns_401():
    async with _adversarial_client(api_key="secret") as client:
        resp = await client.post(
            "/review-adversarial",
            json={"diff_text": _SAMPLE_DIFF, "first_pass_output": _FIRST_PASS_OUTPUT},
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401


async def test_http_adversarial_review_no_key_set_allows_all():
    async with _adversarial_client(api_key=None) as client:
        resp = await client.post("/review-adversarial", json={
            "diff_text": _SAMPLE_DIFF,
            "first_pass_output": _FIRST_PASS_OUTPUT,
        })
    assert resp.status_code == 200


async def test_mcp_adversarial_review_tool_reachable_in_process():
    """adversarial_review MCP tool is callable directly (not via HTTP) with mocks."""
    review_server = _load_review_server()
    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    class _MockLLM:
        provider_name = "ollama"
        model_name = "test-model"

        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            return LLMResponse(content=_VALID_CRITIC_OUTPUT)

    with (
        patch.object(review_server, "_build_llm_provider", return_value=_MockLLM()),
        patch.object(review_server, "GatewayClient", return_value=mock_gateway),
        patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}),
    ):
        result = await review_server._run_adversarial_review(
            _SAMPLE_DIFF, _FIRST_PASS_OUTPUT, review_server._DEFAULT_ADVERSARIAL_TASK, None,
        )
    assert result["findings"][0]["outcome"] == "confirmed"
