"""Skill execution workflow — extracted from GatewayClient.

A SkillRunner fetches a promoted skill from governance, then walks its steps,
invoking each tool via the supplied GatewayClient. The gateway performs the
per-step OPA check (via its governance sidecar) on every `call_tool`, so
revoked or denied steps surface here as `ToolAccessDenied` and the runner
applies the step's `on_failure` policy (ABORT / CONTINUE / ROLLBACK).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

import httpx

from harness_gateway.client import ToolAccessDenied

if TYPE_CHECKING:
    from harness_gateway.client import GatewayClient

logger = logging.getLogger(__name__)


class SkillRunner:
    def __init__(self, gateway: "GatewayClient") -> None:
        self.gateway = gateway

    async def execute(self, skill_id: str, inputs: dict | None = None) -> dict:
        """Execute a promoted skill step-by-step with per-step OPA re-check."""
        if not self.gateway.governance_url:
            raise RuntimeError("SkillRunner requires gateway.governance_url to be set")
        skill = await self._fetch_skill(skill_id)
        steps = self._parse_steps(skill)
        inputs = inputs or {}
        results: list = []
        for step in steps:
            try:
                results.append(await self._execute_step(step, inputs))
            except ToolAccessDenied as exc:
                if not await self._handle_step_failure(exc, step, inputs, results):
                    raise
        return {
            "skill_id": skill_id,
            "steps_completed": self._count_completed(results),
            "results": results,
        }

    async def _fetch_skill(self, skill_id: str) -> dict:
        token = await self.gateway.get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.gateway.governance_url}/skills/{skill_id}",
                headers=headers,
                timeout=10.0,
            )
        if resp.status_code == 410:
            raise ToolAccessDenied(f"skill {skill_id!r} is revoked")
        if resp.status_code == 404:
            raise ToolAccessDenied(f"skill {skill_id!r} not found")
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _parse_steps(skill: dict) -> list:
        steps = skill["steps"]
        return json.loads(steps) if isinstance(steps, str) else steps

    async def _execute_step(self, step: dict, inputs: dict) -> dict:
        action = step.get("action") or step.get("tool", "")
        result = await self.gateway.call_tool(action, inputs)
        expected = step.get("expected_signal")
        if expected and isinstance(result, dict):
            if not all(k in result for k in expected):
                raise ToolAccessDenied(
                    f"signal mismatch on step {action!r}: expected keys {list(expected)}"
                )
        return {"step": action, "result": result}

    async def _run_rollback(self, rollback_steps: list, inputs: dict) -> None:
        for rs in rollback_steps:
            action = rs.get("action") if isinstance(rs, dict) else rs
            try:
                await self.gateway.call_tool(action, inputs)
            except Exception as e:
                logger.warning("rollback step %s failed: %s", action, e)

    async def _handle_step_failure(
        self, exc: ToolAccessDenied, step: dict, inputs: dict, results: list
    ) -> bool:
        """Apply on_failure policy. Returns True to continue, False to re-raise."""
        on_failure = step.get("on_failure", "ABORT")
        if on_failure == "CONTINUE":
            logger.warning("step %s denied, continuing: %s", step.get("action"), exc)
            results.append({"step": step.get("action"), "skipped": True, "error": str(exc)})
            return True
        if on_failure == "ROLLBACK":
            await self._run_rollback(step.get("rollback_steps", []), inputs)
        return False

    @staticmethod
    def _count_completed(results: list) -> int:
        return sum(1 for r in results if not r.get("skipped"))
