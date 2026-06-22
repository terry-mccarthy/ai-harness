"""Unit tests for LLM usage reporting via GatewayClient.

Covers report_llm_usage() and the DynamicSREAgent integration point.
No Docker stack required.
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# GatewayClient.report_llm_usage
# ---------------------------------------------------------------------------

async def test_report_llm_usage_posts_correct_payload():
    """report_llm_usage sends provider, model, and token counts to /audit."""
    from harness_gateway.client import GatewayClient

    posted: list[dict] = []

    async def _fake_post(url, json=None, headers=None, timeout=None):
        posted.append({"url": url, "body": json})
        resp = MagicMock()
        resp.status_code = 202
        return resp

    gw = GatewayClient(
        gateway_url="http://mcpjungle:8080",
        governance_url="http://governance:8090",
        client_id="sre",
        client_secret="secret",
    )

    with patch.object(gw, "get_token", return_value="tok123"), \
         patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__ = AsyncMock(return_value=MagicMock(post=AsyncMock(side_effect=_fake_post)))
        mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
        await gw.report_llm_usage(
            provider="ollama",
            model="qwen2.5-coder:7b",
            prompt_tokens=1200,
            completion_tokens=300,
        )

    assert len(posted) == 1
    body = posted[0]["body"]
    assert body["tool_name"] == "__llm__"
    assert body["llm_provider"] == "ollama"
    assert body["llm_model"] == "qwen2.5-coder:7b"
    assert body["llm_tokens"] == {"prompt": 1200, "completion": 300}
    assert posted[0]["url"] == "http://governance:8090/audit"


async def test_report_llm_usage_noop_when_no_governance():
    """report_llm_usage is a no-op when governance_url is None."""
    from harness_gateway.client import GatewayClient

    gw = GatewayClient(
        gateway_url="http://mcpjungle:8080",
        client_id="sre",
        client_secret="secret",
        governance_url=None,
    )

    # Should complete without making any HTTP calls
    await gw.report_llm_usage("ollama", "qwen2.5-coder:7b", 100, 50)


async def test_report_llm_usage_swallows_exceptions():
    """Failure to reach governance does not propagate."""
    from harness_gateway.client import GatewayClient

    gw = GatewayClient(
        gateway_url="http://mcpjungle:8080",
        governance_url="http://governance:8090",
        client_id="sre",
        client_secret="secret",
    )

    with patch.object(gw, "get_token", side_effect=Exception("token failure")):
        # Must not raise
        await gw.report_llm_usage("ollama", "qwen2.5-coder:7b", 100, 50)


# ---------------------------------------------------------------------------
# DynamicSREAgent — LLM usage reporting after run()
# ---------------------------------------------------------------------------

class _MockLLM:
    provider_name = "ollama"
    model_name = "qwen2.5-coder:7b"
    _turn = 0

    def __init__(self, response: str):
        self._response = response

    async def chat(self, messages):
        from harness_agents.llm import LLMResponse
        return LLMResponse(content=self._response, prompt_tokens=100, completion_tokens=50)


class _RecordingGateway:
    """Gateway that records report_llm_usage calls."""
    def __init__(self):
        self.llm_reports: list[dict] = []

    async def call_tool(self, name, params):
        return {"result": "stub"}

    async def report_llm_usage(self, provider, model, prompt_tokens, completion_tokens):
        self.llm_reports.append({
            "provider": provider, "model": model,
            "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
        })


def _respond_report():
    import json
    return json.dumps({"action": "respond", "result": {
        "timeline": "t", "likely_cause": "c", "severity": "P3",
        "recommended_steps": [], "runbook_ref": None, "requires_human_approval": False,
    }})


def _make_state():
    import uuid
    return {
        "task": "Test incident",
        "thread_id": str(uuid.uuid4()),
        "diff": "",
        "agent_output": None,
        "requires_human_approval": False,
        "error": None,
    }


async def test_llm_usage_reported_after_successful_run():
    """After a successful run, report_llm_usage is called with accumulated counts."""
    from harness_agents.dynamic_sre import DynamicSREAgent

    gw = _RecordingGateway()
    llm = _MockLLM(_respond_report())

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_make_state())

    assert result.get("error") is None
    assert len(gw.llm_reports) == 1
    r = gw.llm_reports[0]
    assert r["provider"] == "ollama"
    assert r["model"] == "qwen2.5-coder:7b"
    assert r["prompt_tokens"] == 100
    assert r["completion_tokens"] == 50


async def test_llm_usage_reported_on_max_turns_exceeded():
    """report_llm_usage is called even when the agent hits the turn limit."""
    import json
    from harness_agents.dynamic_sre import DynamicSREAgent, MAX_TURNS

    class _InfiniteGateway(_RecordingGateway):
        pass

    gw = _InfiniteGateway()
    # Always call a tool, never respond → max_turns_exceeded
    turns = [json.dumps({"action": "call_tool", "tool": "observability_query", "params": {"query": "x"}})] * (MAX_TURNS + 1)
    llm = _MockLLM(turns[0])
    llm._turns = turns
    llm._idx = 0

    async def _multi_chat(messages):
        from harness_agents.llm import LLMResponse
        content = llm._turns[llm._idx % len(llm._turns)]
        llm._idx += 1
        return LLMResponse(content=content, prompt_tokens=10, completion_tokens=5)

    llm.chat = _multi_chat

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_make_state())

    assert result["error"]["code"] == "max_turns_exceeded"
    assert len(gw.llm_reports) == 1
    assert gw.llm_reports[0]["prompt_tokens"] > 0


async def test_llm_usage_not_reported_when_gateway_lacks_method():
    """If gateway has no report_llm_usage, run() completes without error."""
    import json
    from harness_agents.dynamic_sre import DynamicSREAgent

    class _BasicGateway:
        async def call_tool(self, name, params):
            return {"result": "stub"}
        # intentionally no report_llm_usage

    gw = _BasicGateway()
    llm = _MockLLM(_respond_report())

    result = await DynamicSREAgent(gateway=gw, llm_provider=llm).run(_make_state())
    assert result.get("error") is None
