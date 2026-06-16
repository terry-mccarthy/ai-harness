"""Unit tests for SkillRunner — extracted from GatewayClient (issue 07).

Covers:
- SkillRunner.execute happy path
- on_failure ABORT / CONTINUE / ROLLBACK
- 410 (revoked) and 404 (missing) skill fetches raise ToolAccessDenied
- Steps stored as JSON string in Dolt are decoded
- expected_signal mismatch raises ToolAccessDenied
- The seven extracted methods are no longer on GatewayClient
- GatewayClient.execute_skill is a thin shim that delegates to SkillRunner
"""

import httpx
import pytest

from harness_gateway.client import GatewayClient, ToolAccessDenied
from harness_gateway.skill_runner import SkillRunner


def _fake_gateway(governance_url: str = "http://gov", call_tool=None) -> GatewayClient:
    gw = GatewayClient(
        gateway_url="http://gw",
        governance_url=governance_url,
        client_id="sre",
        client_secret="",
    )
    if call_tool is not None:
        gw.call_tool = call_tool  # type: ignore[method-assign]
    return gw


def _mock_skill_response(skill_steps, status: int = 200):
    body = {"id": "test", "steps": skill_steps}

    async def mock_get(self, url, **kwargs):
        request = httpx.Request("GET", str(url))
        return httpx.Response(status, json=body if status == 200 else {}, request=request)

    return mock_get


@pytest.mark.asyncio
async def test_execute_runs_all_steps(monkeypatch):
    calls: list[str] = []

    async def call_tool(tool, params):
        calls.append(tool)
        return {"ok": True}

    monkeypatch.setattr(
        httpx.AsyncClient, "get",
        _mock_skill_response([{"action": "log_search"}, {"action": "observability_query"}]),
    )
    gw = _fake_gateway(call_tool=call_tool)
    result = await SkillRunner(gw).execute("test:multi", {})
    assert calls == ["log_search", "observability_query"]
    assert result["steps_completed"] == 2
    assert len(result["results"]) == 2


@pytest.mark.asyncio
async def test_abort_on_step_denial_reraises(monkeypatch):
    async def call_tool(tool, params):
        if tool == "codebase_search":
            raise ToolAccessDenied("403 Forbidden: codebase_search")
        return {"ok": True}

    monkeypatch.setattr(
        httpx.AsyncClient, "get",
        _mock_skill_response([
            {"action": "log_search", "on_failure": "ABORT"},
            {"action": "codebase_search", "on_failure": "ABORT"},
            {"action": "observability_query", "on_failure": "ABORT"},
        ]),
    )
    gw = _fake_gateway(call_tool=call_tool)
    with pytest.raises(ToolAccessDenied):
        await SkillRunner(gw).execute("test:abort", {})


@pytest.mark.asyncio
async def test_continue_skips_denied_step(monkeypatch):
    async def call_tool(tool, params):
        if tool == "codebase_search":
            raise ToolAccessDenied("403")
        return {"ok": True}

    monkeypatch.setattr(
        httpx.AsyncClient, "get",
        _mock_skill_response([
            {"action": "log_search", "on_failure": "CONTINUE"},
            {"action": "codebase_search", "on_failure": "CONTINUE"},
            {"action": "observability_query", "on_failure": "CONTINUE"},
        ]),
    )
    gw = _fake_gateway(call_tool=call_tool)
    result = await SkillRunner(gw).execute("test:cont", {})
    skipped = [r for r in result["results"] if r.get("skipped")]
    assert len(skipped) == 1
    assert result["steps_completed"] == 2
    assert len(result["results"]) == 3


@pytest.mark.asyncio
async def test_rollback_runs_rollback_steps_then_raises(monkeypatch):
    invoked: list[str] = []

    async def call_tool(tool, params):
        invoked.append(tool)
        if tool == "codebase_search":
            raise ToolAccessDenied("403")
        return {"ok": True}

    monkeypatch.setattr(
        httpx.AsyncClient, "get",
        _mock_skill_response([{
            "action": "codebase_search",
            "on_failure": "ROLLBACK",
            "rollback_steps": [{"action": "log_search"}],
        }]),
    )
    gw = _fake_gateway(call_tool=call_tool)
    with pytest.raises(ToolAccessDenied):
        await SkillRunner(gw).execute("test:rb", {})
    assert "log_search" in invoked


@pytest.mark.asyncio
async def test_revoked_skill_raises(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", _mock_skill_response([], status=410))
    gw = _fake_gateway()
    with pytest.raises(ToolAccessDenied, match="revoked"):
        await SkillRunner(gw).execute("test:revoked", {})


@pytest.mark.asyncio
async def test_missing_skill_raises(monkeypatch):
    monkeypatch.setattr(httpx.AsyncClient, "get", _mock_skill_response([], status=404))
    gw = _fake_gateway()
    with pytest.raises(ToolAccessDenied, match="not found"):
        await SkillRunner(gw).execute("test:missing", {})


@pytest.mark.asyncio
async def test_steps_decoded_when_stored_as_json_string(monkeypatch):
    """Dolt stores steps as a JSON string; SkillRunner must json.loads it."""
    import json
    json_steps = json.dumps([{"action": "log_search"}])

    async def call_tool(tool, params):
        return {"ok": True}

    monkeypatch.setattr(
        httpx.AsyncClient, "get", _mock_skill_response(json_steps),
    )
    gw = _fake_gateway(call_tool=call_tool)
    result = await SkillRunner(gw).execute("test:str", {})
    assert result["steps_completed"] == 1


@pytest.mark.asyncio
async def test_expected_signal_mismatch_raises(monkeypatch):
    async def call_tool(tool, params):
        return {"only_this_key": True}

    monkeypatch.setattr(
        httpx.AsyncClient, "get",
        _mock_skill_response([
            {"action": "log_search", "expected_signal": {"required_key": True}, "on_failure": "ABORT"},
        ]),
    )
    gw = _fake_gateway(call_tool=call_tool)
    with pytest.raises(ToolAccessDenied, match="signal mismatch"):
        await SkillRunner(gw).execute("test:sig", {})


@pytest.mark.asyncio
async def test_execute_requires_governance_url():
    gw = _fake_gateway(governance_url=None)
    with pytest.raises(RuntimeError, match="governance"):
        await SkillRunner(gw).execute("test", {})


def test_extracted_methods_are_not_on_gateway_client():
    """The seven private skill-execution methods must live on SkillRunner, not GatewayClient."""
    removed = [
        "_fetch_skill",
        "_parse_steps",
        "_execute_step",
        "_handle_step_failure",
        "_run_rollback",
        "_count_completed",
    ]
    for name in removed:
        assert not hasattr(GatewayClient, name), f"GatewayClient still defines {name}"


@pytest.mark.asyncio
async def test_gateway_execute_skill_shim_delegates_to_runner(monkeypatch):
    """The compat shim on GatewayClient must defer to SkillRunner.execute."""
    captured = {}

    async def fake_execute(self, skill_id, inputs):
        captured["skill_id"] = skill_id
        captured["inputs"] = inputs
        return {"shim": True}

    monkeypatch.setattr(SkillRunner, "execute", fake_execute)
    gw = _fake_gateway()
    out = await gw.execute_skill("test:shim", {"x": 1})
    assert captured == {"skill_id": "test:shim", "inputs": {"x": 1}}
    assert out == {"shim": True}
