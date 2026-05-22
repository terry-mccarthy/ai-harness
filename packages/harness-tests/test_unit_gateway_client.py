"""Unit tests for GatewayClient.call_tool response unwrapping and error handling."""

import httpx
import pytest
from harness_gateway.client import GatewayClient, ToolAccessDenied


async def _mock_post_factory(status=200, json_body=None):
    async def mock_post(self, url, **kwargs):
        request = httpx.Request("POST", str(url))
        return httpx.Response(status, json=json_body or {}, request=request)
    return mock_post


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
