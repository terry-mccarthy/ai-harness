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
async def _review_client(llm_response: str = _VALID_REVIEW, api_key: str | None = None):
    """Yield an httpx AsyncClient wired to the review server ASGI app.

    GatewayClient and LLMProvider are replaced with in-process mocks for the
    duration of the context, so no Docker stack is required.

    Pass api_key to simulate REVIEW_API_KEY being set in the environment.
    """
    import server as review_server

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
        patch("server.GatewayClient", return_value=mock_gateway),
        patch.dict("os.environ", env, clear=False),
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


async def test_http_review_agent_error_returns_400():
    """Agent-level errors (invalid LLM output) return 400, not 500."""
    import server as review_server

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
        patch("server.GatewayClient", return_value=mock_gateway),
        patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/review", json={"diff_text": _SAMPLE_DIFF})

    assert resp.status_code == 400
    body = resp.json()
    assert "max retries" in body.get("error", "").lower()


async def test_http_review_missing_provider_ollama_no_host_falls_back_to_env():
    """Ollama provider is built successfully when no host override is given."""
    import server as review_server

    mock_gateway = MagicMock()
    mock_gateway.call_tool = AsyncMock(return_value={"result": "ok"})

    class _MockLLM:
        provider_name = "ollama"
        model_name = "test-model"

        async def chat(self, messages):
            from harness_agents.llm import LLMResponse
            return LLMResponse(content=_VALID_REVIEW)

    app = review_server.mcp.streamable_http_app()
    with (
        patch.object(review_server, "_build_llm_provider", return_value=_MockLLM()),
        patch("server.GatewayClient", return_value=mock_gateway),
        patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/review", json={"diff_text": _SAMPLE_DIFF})

    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Slice 5 — API key authentication
# ---------------------------------------------------------------------------

async def test_http_review_no_key_set_allows_all():
    """When REVIEW_API_KEY is unset any request is allowed (dev/local mode)."""
    async with _review_client(api_key=None) as client:
        resp = await client.post("/review", json={"diff_text": _SAMPLE_DIFF})
    assert resp.status_code == 200


async def test_http_review_correct_key_allows_request():
    """Correct bearer token passes auth check."""
    async with _review_client(api_key="secret-token") as client:
        resp = await client.post(
            "/review",
            json={"diff_text": _SAMPLE_DIFF},
            headers={"Authorization": "Bearer secret-token"},
        )
    assert resp.status_code == 200


async def test_http_review_wrong_key_returns_401():
    """Wrong bearer token is rejected with 401."""
    async with _review_client(api_key="secret-token") as client:
        resp = await client.post(
            "/review",
            json={"diff_text": _SAMPLE_DIFF},
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert resp.status_code == 401


async def test_http_review_missing_header_returns_401():
    """No Authorization header when key is required returns 401."""
    async with _review_client(api_key="secret-token") as client:
        resp = await client.post("/review", json={"diff_text": _SAMPLE_DIFF})
    assert resp.status_code == 401


async def test_http_review_malformed_header_returns_401():
    """Authorization header present but not 'Bearer <token>' format returns 401."""
    async with _review_client(api_key="secret-token") as client:
        resp = await client.post(
            "/review",
            json={"diff_text": _SAMPLE_DIFF},
            headers={"Authorization": "secret-token"},  # missing "Bearer " prefix
        )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Slice 6 — Config API
# ---------------------------------------------------------------------------

async def test_config_get_returns_effective_config():
    """GET /config returns the effective config (env defaults merged with overrides)."""
    import server as review_server
    app = review_server.mcp.streamable_http_app()
    with patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/config")
    assert resp.status_code == 200
    body = resp.json()
    # llm_provider is now always resolved to a concrete value (default "ollama"),
    # never the bare None override.
    assert body["llm_provider"]
    assert "ollama" in body and "gemini" in body and "openrouter" in body
    # provider sub-dicts carry their resolved settings, not an empty override slot
    assert "model" in body["ollama"]
    assert "host" in body["ollama"]


async def test_config_get_sanitizes_api_keys():
    """Sensitive keys are masked in the response."""
    import server as review_server
    review_server._CONFIG["openrouter"]["api_key"] = "sk-or-v1-abcdef1234567890"
    review_server._CONFIG["gemini"]["api_key"] = "AIzaSyD-test-key-12345"
    try:
        app = review_server.mcp.streamable_http_app()
        with patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.get("/config")
        assert resp.status_code == 200
        body = resp.json()
        # api_key values should be masked (partial display with "...")
        assert "..." in body["openrouter"]["api_key"]
        assert "sk-o" in body["openrouter"]["api_key"]
        assert "..." in body["gemini"]["api_key"]
        assert "AIza" in body["gemini"]["api_key"]
    finally:
        # restore clean state
        review_server._CONFIG["openrouter"].pop("api_key", None)
        review_server._CONFIG["gemini"].pop("api_key", None)


async def test_config_put_updates_ollama_model():
    """PUT /config updates a provider key and is reflected in GET."""
    import server as review_server
    app = review_server.mcp.streamable_http_app()
    with patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put("/config", json={"ollama": {"model": "qwen2.5-coder:32b"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["config"]["ollama"]["model"] == "qwen2.5-coder:32b"
    # verify side-effect on module global
    assert review_server._get_cfg("ollama", "model") == "qwen2.5-coder:32b"


async def test_config_put_clears_key_on_null():
    """Setting a config key to null removes the override."""
    import server as review_server
    review_server._CONFIG["ollama"]["model"] = "some-model"
    try:
        app = review_server.mcp.streamable_http_app()
        with patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                resp = await client.put("/config", json={"ollama": {"model": None}})
        assert resp.status_code == 200
        assert review_server._get_cfg("ollama", "model") is None
    finally:
        review_server._CONFIG["ollama"].pop("model", None)


# ---------------------------------------------------------------------------
# Slice — POST /review-architecture (plain HTTP, no MCP client timeout)
# ---------------------------------------------------------------------------

_ARCH_RESULT = {"compliant": True, "findings": [], "summary": "ok"}


async def test_http_architecture_review_happy_path():
    """POST /review-architecture returns the architecture_review result as JSON."""
    with patch("architecture_review.architecture_review", AsyncMock(return_value=_ARCH_RESULT)):
        async with _review_client() as client:
            resp = await client.post(
                "/review-architecture",
                json={"target_mode": "codebase", "repo": "https://github.com/o/r"},
            )
    assert resp.status_code == 200
    assert resp.json() == _ARCH_RESULT


async def test_http_architecture_review_missing_target_mode_returns_422():
    async with _review_client() as client:
        resp = await client.post("/review-architecture", json={"repo": "https://github.com/o/r"})
    assert resp.status_code == 422


async def test_http_architecture_review_missing_repo_returns_422():
    async with _review_client() as client:
        resp = await client.post("/review-architecture", json={"target_mode": "codebase"})
    assert resp.status_code == 422


async def test_http_architecture_review_wrong_key_returns_401():
    """When REVIEW_API_KEY is set, a wrong bearer token is rejected before any work."""
    async with _review_client(api_key="secret") as client:
        resp = await client.post(
            "/review-architecture",
            json={"target_mode": "codebase", "repo": "https://github.com/o/r"},
            headers={"Authorization": "Bearer wrong"},
        )
    assert resp.status_code == 401


async def test_config_put_updates_llm_provider():
    """PUT can change the active llm_provider."""
    import server as review_server
    app = review_server.mcp.streamable_http_app()
    with patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put("/config", json={"llm_provider": "openrouter"})
    assert resp.status_code == 200
    assert review_server._CONFIG["llm_provider"] == "openrouter"
    # reset
    review_server._CONFIG["llm_provider"] = None


async def test_config_put_invalid_json_returns_422():
    """Malformed body returns 422."""
    import server as review_server
    app = review_server.mcp.streamable_http_app()
    with patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put("/config", content=b"not-json", headers={"Content-Type": "application/json"})
    assert resp.status_code == 422


async def test_config_get_respects_auth():
    """GET /config respects REVIEW_API_KEY."""
    import server as review_server
    app = review_server.mcp.streamable_http_app()
    with patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080", "REVIEW_API_KEY": "sekret"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/config")
    assert resp.status_code == 401


async def test_config_put_respects_auth():
    """PUT /config respects REVIEW_API_KEY."""
    import server as review_server
    app = review_server.mcp.streamable_http_app()
    with patch.dict("os.environ", {"MCPJUNGLE_URL": "http://mock-jungle:8080", "REVIEW_API_KEY": "sekret"}):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.put("/config", json={"ollama": {"model": "x"}})
    assert resp.status_code == 401
