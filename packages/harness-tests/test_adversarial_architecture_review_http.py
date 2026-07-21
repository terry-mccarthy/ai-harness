"""Unit tests for the review server's POST /review-architecture-adversarial endpoint
and the adversarial_architecture_review MCP tool wiring. Mirrors
test_adversarial_review_http.py's approach: GatewayClient and LLMProvider mocked
in-process, no Docker stack needed.
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
            "severity": "HIGH",
            "location": "shopflow/routes.py",
            "message": "business logic inline in the route handler",
            "regression_scenario": "adding a second payment provider requires editing every route handler",
        }
    ],
    "summary": "Confirmed with a concrete regression trace.",
})

_REPO = "https://github.com/example/shopflow"
_FIRST_PASS_OUTPUT = {
    "title": "Architecture Review: shopflow",
    "status": "completed",
    "summary": "first pass",
    "findings": [],
    "recommendations": [],
}


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


async def test_http_adversarial_architecture_review_endpoint_exists():
    async with _adversarial_client() as client:
        resp = await client.post("/review-architecture-adversarial", json={
            "repo": _REPO,
            "first_pass_output": _FIRST_PASS_OUTPUT,
        })
    assert resp.status_code == 200


async def test_http_adversarial_architecture_review_returns_findings_and_summary():
    async with _adversarial_client() as client:
        resp = await client.post("/review-architecture-adversarial", json={
            "repo": _REPO,
            "first_pass_output": _FIRST_PASS_OUTPUT,
        })
    body = resp.json()
    assert "findings" in body
    assert "summary" in body
    assert body["findings"][0]["outcome"] == "confirmed"
    assert body["findings"][0]["regression_scenario"]


async def test_http_adversarial_architecture_review_missing_repo_returns_422():
    async with _adversarial_client() as client:
        resp = await client.post("/review-architecture-adversarial", json={"first_pass_output": _FIRST_PASS_OUTPUT})
    assert resp.status_code == 422


async def test_http_adversarial_architecture_review_missing_first_pass_output_returns_422():
    async with _adversarial_client() as client:
        resp = await client.post("/review-architecture-adversarial", json={"repo": _REPO})
    assert resp.status_code == 422


async def test_http_adversarial_architecture_review_empty_dict_first_pass_output_is_accepted():
    """{} is a valid (if degenerate) dict, not a missing field — must not 422."""
    async with _adversarial_client() as client:
        resp = await client.post("/review-architecture-adversarial", json={
            "repo": _REPO,
            "first_pass_output": {},
        })
    assert resp.status_code == 200


async def test_http_adversarial_architecture_review_agent_error_returns_400():
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
            resp = await client.post("/review-architecture-adversarial", json={
                "repo": _REPO,
                "first_pass_output": _FIRST_PASS_OUTPUT,
            })

    assert resp.status_code == 400
    assert "max retries" in resp.json().get("error", "").lower()


async def test_http_adversarial_architecture_review_wrong_key_returns_401():
    async with _adversarial_client(api_key="secret") as client:
        resp = await client.post(
            "/review-architecture-adversarial",
            json={"repo": _REPO, "first_pass_output": _FIRST_PASS_OUTPUT},
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401


async def test_http_adversarial_architecture_review_no_key_set_allows_all():
    async with _adversarial_client(api_key=None) as client:
        resp = await client.post("/review-architecture-adversarial", json={
            "repo": _REPO,
            "first_pass_output": _FIRST_PASS_OUTPUT,
        })
    assert resp.status_code == 200


async def test_http_adversarial_architecture_review_accepts_diff_target_mode():
    """Mirrors POST /review-architecture: target_mode='diff' + diff is a valid target shape."""
    async with _adversarial_client() as client:
        resp = await client.post("/review-architecture-adversarial", json={
            "repo": _REPO,
            "first_pass_output": _FIRST_PASS_OUTPUT,
            "target_mode": "diff",
            "diff": "diff --git a/x.py b/x.py\n+x=1",
        })
    assert resp.status_code == 200


async def test_http_adversarial_architecture_review_defaults_to_codebase_target_mode():
    """Omitting target_mode/diff entirely (codebase mode) still works — mirrors /review-architecture's default."""
    async with _adversarial_client() as client:
        resp = await client.post("/review-architecture-adversarial", json={
            "repo": _REPO,
            "first_pass_output": _FIRST_PASS_OUTPUT,
        })
    assert resp.status_code == 200


async def test_http_adversarial_architecture_review_diff_reaches_the_critic_prompt():
    """The diff body isn't just accepted and dropped — it's threaded into the agent's prompt."""
    review_server = _load_review_server()
    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})
    captured = {}

    class _CapturingLLM:
        provider_name = "ollama"
        model_name = "test-model"

        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            captured["messages"] = messages
            return LLMResponse(content=_VALID_CRITIC_OUTPUT)

    diff_text = "diff --git a/x.py b/x.py\n+x=1"
    app = review_server.mcp.streamable_http_app()
    with (
        patch.object(review_server, "_build_llm_provider", return_value=_CapturingLLM()),
        patch.object(review_server, "GatewayClient", return_value=mock_gateway),
        patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/review-architecture-adversarial", json={
                "repo": _REPO,
                "first_pass_output": _FIRST_PASS_OUTPUT,
                "target_mode": "diff",
                "diff": diff_text,
            })

    assert resp.status_code == 200
    assert diff_text in captured["messages"][-1]["content"]


async def test_mcp_adversarial_architecture_review_tool_reachable_in_process():
    """adversarial_architecture_review MCP tool is callable directly (not via HTTP) with mocks."""
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
        result = await review_server._run_adversarial_architecture_review(
            _REPO, _FIRST_PASS_OUTPUT, review_server._DEFAULT_ADVERSARIAL_ARCHITECTURE_TASK, None,
        )
    assert result["findings"][0]["outcome"] == "confirmed"
