"""Unit tests for GatewayClient.call_tool response unwrapping and error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from harness_gateway.client import GatewayClient, ToolAccessDenied


async def _mock_post_factory(status=200, json_body=None):
    async def mock_post(self, url, **kwargs):
        request = httpx.Request("POST", str(url))
        return httpx.Response(status, json=json_body or {}, request=request)
    return mock_post


def _capturing_mock(token="test-token", invoke_status=200, invoke_body=None):
    """Returns (mock_fn, captured) where captured['headers'] is set after call."""
    captured = {"headers": {}}
    invoke_body = invoke_body or {"content": [{"type": "text", "text": "{}"}]}

    async def mock_post(self, url, **kwargs):
        request = httpx.Request("POST", str(url))
        if "/oauth/token" in str(url):
            return httpx.Response(
                200,
                json={"access_token": token, "expires_in": 900},
                request=request,
            )
        captured["headers"] = dict(kwargs.get("headers") or {})
        return httpx.Response(invoke_status, json=invoke_body, request=request)

    return mock_post, captured


@pytest.mark.asyncio
async def test_parses_json_content_in_mcp_response(monkeypatch):
    body = {"content": [{"type": "text", "text": '{"verdict": "pass", "findings": [], "summary": "ok"}'}]}
    monkeypatch.setattr(httpx.AsyncClient, "post", await _mock_post_factory(json_body=body))

    client = GatewayClient(gateway_url="http://test", client_id="test", client_secret="")
    result = await client.call_tool("git_diff", {})
    assert result == {"verdict": "pass", "findings": [], "summary": "ok"}


@pytest.mark.asyncio
async def test_returns_raw_text_when_content_is_not_json(monkeypatch):
    body = {"content": [{"type": "text", "text": "plain text response"}]}
    monkeypatch.setattr(httpx.AsyncClient, "post", await _mock_post_factory(json_body=body))

    client = GatewayClient(gateway_url="http://test", client_id="test", client_secret="")
    result = await client.call_tool("git_diff", {})
    assert result == "plain text response"


@pytest.mark.asyncio
async def test_non_text_content_type_returns_raw_data(monkeypatch):
    body = {"content": [{"type": "image", "text": "..."}]}
    monkeypatch.setattr(httpx.AsyncClient, "post", await _mock_post_factory(json_body=body))

    client = GatewayClient(gateway_url="http://test", client_id="test", client_secret="")
    result = await client.call_tool("git_diff", {})
    assert result == body


@pytest.mark.asyncio
async def test_result_string_is_not_unwrapped(monkeypatch):
    """A string result is not a list, so the function returns the raw data dict."""
    body = {"result": "flat_string"}
    monkeypatch.setattr(httpx.AsyncClient, "post", await _mock_post_factory(json_body=body))

    client = GatewayClient(gateway_url="http://test", client_id="test", client_secret="")
    result = await client.call_tool("git_diff", {})
    assert result == body


@pytest.mark.asyncio
async def test_empty_content_falls_through_to_result(monkeypatch):
    """Content [] is falsy, so it falls back to result — which is a string,
    so items[0] is a character, which is not a dict → raw data returned."""
    monkeypatch.setattr(
        httpx.AsyncClient, "post",
        await _mock_post_factory(json_body={"content": [], "result": "from_result"}),
    )

    client = GatewayClient(gateway_url="http://test", client_id="test", client_secret="")
    result = await client.call_tool("git_diff", {})
    assert result == {"content": [], "result": "from_result"}


@pytest.mark.asyncio
async def test_missing_content_and_result_returns_raw_data(monkeypatch):
    body = {"unexpected": "shape"}
    monkeypatch.setattr(httpx.AsyncClient, "post", await _mock_post_factory(json_body=body))

    client = GatewayClient(gateway_url="http://test", client_id="test", client_secret="")
    result = await client.call_tool("git_diff", {})
    assert result == body


@pytest.mark.asyncio
async def test_authorization_header_sent_when_secret_set(monkeypatch):
    mock, captured = _capturing_mock(token="tok-abc")
    monkeypatch.setattr(httpx.AsyncClient, "post", mock)

    client = GatewayClient(gateway_url="http://test", client_id="ci", client_secret="secret")
    await client.call_tool("git_diff", {})

    assert captured["headers"].get("Authorization") == "Bearer tok-abc"


@pytest.mark.asyncio
async def test_no_authorization_header_when_no_secret(monkeypatch):
    mock, captured = _capturing_mock()
    monkeypatch.setattr(httpx.AsyncClient, "post", mock)

    client = GatewayClient(gateway_url="http://test", client_id="ci", client_secret="")
    await client.call_tool("git_diff", {})

    assert "Authorization" not in captured["headers"]


@pytest.mark.asyncio
async def test_unknown_tool_raises_error():
    client = GatewayClient(gateway_url="http://test", client_id="test", client_secret="")
    with pytest.raises(ToolAccessDenied, match="not in allowed"):
        await client.call_tool("nonexistent_tool", {})


@pytest.mark.asyncio
async def test_403_raises_tool_access_denied(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "post", await _mock_post_factory(status=403))

    client = GatewayClient(gateway_url="http://test", client_id="test", client_secret="")
    with pytest.raises(ToolAccessDenied, match="403"):
        await client.call_tool("git_diff", {})


@pytest.mark.asyncio
async def test_401_raises_tool_access_denied(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "post", await _mock_post_factory(status=401))

    client = GatewayClient(gateway_url="http://test", client_id="test", client_secret="")
    with pytest.raises(ToolAccessDenied, match="401"):
        await client.call_tool("git_diff", {})


# ---------------------------------------------------------------------------
# GatewayClient._governance_check — thread_id plumbing (issue #01)
#
# Governance's /check now cryptographically validates the shell_exec approval
# token against (thread_id, tool_name). thread_id has to reach that request
# body somehow — this is the "somehow": a mutable instance field mirroring the
# existing human_approval_token convention.
# ---------------------------------------------------------------------------


async def _run_governance_check(gw: GatewayClient, tool_name: str) -> dict | None:
    """Invoke _governance_check and return the JSON body it posted to /check."""
    posted: list[dict] = []

    async def _fake_post(url, json=None, headers=None, timeout=None):
        posted.append({"url": url, "body": json})
        resp = MagicMock()
        resp.status_code = 200
        return resp

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__ = AsyncMock(
            return_value=MagicMock(post=AsyncMock(side_effect=_fake_post))
        )
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
        await gw._governance_check("tok123", tool_name)

    assert posted[0]["url"] == f"{gw.governance_url}/check"
    return posted[0]["body"]


@pytest.mark.asyncio
async def test_governance_check_includes_thread_id_when_set():
    gw = GatewayClient(
        gateway_url="http://mcpjungle:8080",
        governance_url="http://governance:8090",
        client_id="sre",
        client_secret="secret",
    )
    gw.thread_id = "thread-abc"

    body = await _run_governance_check(gw, "sre_stub__shell_exec")

    assert body == {"tool_name": "sre_stub__shell_exec", "thread_id": "thread-abc"}


@pytest.mark.asyncio
async def test_governance_check_omits_thread_id_when_unset():
    gw = GatewayClient(
        gateway_url="http://mcpjungle:8080",
        governance_url="http://governance:8090",
        client_id="sre",
        client_secret="secret",
    )

    body = await _run_governance_check(gw, "sre_stub__shell_exec")

    assert body == {"tool_name": "sre_stub__shell_exec"}
    assert "thread_id" not in body
