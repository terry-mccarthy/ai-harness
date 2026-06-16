import asyncio
import hashlib
import json
import logging
import time
import uuid as _uuid
from dataclasses import dataclass, field

import httpx
import jwt as pyjwt

logger = logging.getLogger(__name__)

# Maps short tool names to MCPJungle's server__tool format
TOOL_NAME_MAP = {
    "git_diff": "git_diff_stub__git_diff",
    "run_linter": "linter_stub__run_linter",
    "review_diff": "review_server__review_diff",
    "codebase_search": "architect_stub__codebase_search",
    "adr_read": "architect_stub__adr_read",
    "adr_write": "architect_stub__adr_write",
    "diagram_gen": "architect_stub__diagram_gen",
    "observability_query": "sre_stub__observability_query",
    "runbook_read": "sre_stub__runbook_read",
    "log_search": "sre_stub__log_search",
    "shell_exec": "sre_stub__shell_exec",
}


class ToolAccessDenied(Exception):
    pass


@dataclass
class GatewayClient:
    gateway_url: str
    client_id: str
    client_secret: str
    # When set, governance handles policy check + audit; gateway_url is called directly.
    # When None, gateway_url is treated as a governance proxy (legacy mode).
    governance_url: str | None = None
    # Required when gateway_backend="contextforge" and governance_url is set
    gateway_backend: str = "mcpjungle"
    cf_jwt_secret: str | None = None
    cf_admin_email: str = "admin@harness.local"
    cf_server_name: str = "harness_all"
    timeout: float = 180.0
    human_approval_token: str | None = None
    last_calls: list = field(default_factory=list, repr=False)
    _token: str | None = field(default=None, init=False, repr=False)
    _token_exp: float = field(default=0.0, init=False, repr=False)
    _cf_server_uuid: str | None = field(default=None, init=False, repr=False)

    def _auth_url(self) -> str:
        return self.governance_url or self.gateway_url

    async def _get_token(self) -> str | None:
        """Fetch a bearer token from the governance (or gateway) /oauth/token endpoint."""
        if not self.client_secret:
            return None
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self._auth_url()}/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=10.0,
                )
            if resp.status_code == 404:
                return None  # no OAuth on this gateway
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + data.get("expires_in", 900)
            logger.debug("fetched token for %s, exp in %ds", self.client_id, data.get("expires_in"))
            return self._token
        except httpx.HTTPStatusError:
            raise
        except Exception as e:
            logger.warning("token fetch failed: %s", e)
            return None

    def _check_status(self, status: int, tool_name: str) -> None:
        if status == 403:
            raise ToolAccessDenied(f"403 Forbidden: {tool_name}")
        if status == 401:
            raise ToolAccessDenied(f"401 Unauthorized: {tool_name}")
        if status >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {status}", request=httpx.Request("POST", self.gateway_url), response=None
            )

    def _extract_content(self, data: dict):
        items = data.get("content") or data.get("result") or []
        if not (items and isinstance(items[0], dict) and items[0].get("type") == "text"):
            return data
        try:
            return json.loads(items[0]["text"])
        except json.JSONDecodeError:
            return items[0]["text"]

    def _unwrap(self, data: dict, status: int, tool_name: str) -> dict:
        logger.debug("tool_call raw response: %s", data)
        self._check_status(status, tool_name)
        return self._extract_content(data)

    async def _post(self, tool_name: str, full_name: str, params: dict, headers: dict) -> "httpx.Response":
        body = {"name": full_name, **params}
        logger.debug("tool_call request: %s", body)
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.gateway_url}/api/v0/tools/invoke",
                json=body,
                headers=headers,
                timeout=self.timeout,
            )
        self.last_calls.append({"tool": tool_name, "status": resp.status_code})
        logger.info("tool_call tool=%s status=%d", tool_name, resp.status_code)
        return resp

    async def _auth_headers(self) -> dict:
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if self.human_approval_token:
            headers["X-Human-Approval-Token"] = self.human_approval_token
        return headers

    def _resolve_name(self, tool_name: str) -> str:
        full_name = TOOL_NAME_MAP.get(tool_name)
        if full_name is None:
            raise ToolAccessDenied(f"403 Forbidden: {tool_name} not in allowed tool list")
        return full_name

    # ------------------------------------------------------------------
    # Governance sidecar methods (used when governance_url is set)
    # ------------------------------------------------------------------

    async def _governance_check(self, token: str, full_name: str) -> None:
        """POST governance /check; raises ToolAccessDenied if denied."""
        headers: dict = {"Authorization": f"Bearer {token}"}
        if self.human_approval_token:
            headers["X-Human-Approval-Token"] = self.human_approval_token
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.governance_url}/check",
                json={"tool_name": full_name},
                headers=headers,
                timeout=10.0,
            )
        if resp.status_code == 403:
            raise ToolAccessDenied(f"403 Forbidden: {full_name}")
        if resp.status_code == 401:
            raise ToolAccessDenied(f"401 Unauthorized: {full_name}")
        resp.raise_for_status()

    async def _governance_audit(
        self, token: str, full_name: str, req_hash: str, resp_hash: str, latency_ms: int
    ) -> None:
        """Fire-and-forget POST to governance /audit."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"{self.governance_url}/audit",
                    json={
                        "tool_name": full_name,
                        "req_hash": req_hash,
                        "resp_hash": resp_hash,
                        "decision": "allow",
                        "latency_ms": latency_ms,
                    },
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=10.0,
                )
        except Exception as e:
            logger.warning("governance audit post failed: %s", e)

    # ------------------------------------------------------------------
    # Direct gateway invocation (MCPJungle or ContextForge)
    # ------------------------------------------------------------------

    def _cf_jwt(self) -> str:
        now = int(time.time())
        return pyjwt.encode(
            {
                "sub": self.cf_admin_email,
                "preferred_username": "admin",
                "iat": now,
                "iss": "mcpgateway",
                "aud": "mcpgateway-api",
                "jti": str(_uuid.uuid4()),
                "exp": now + 3600,
            },
            self.cf_jwt_secret,
            algorithm="HS256",
        )

    async def _get_cf_server_uuid(self) -> str:
        if self._cf_server_uuid:
            return self._cf_server_uuid
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.gateway_url}/servers",
                headers={"Authorization": f"Bearer {self._cf_jwt()}", "Accept": "application/json"},
                timeout=10.0,
            )
            resp.raise_for_status()
            for srv in resp.json():
                if srv.get("name") == self.cf_server_name:
                    self._cf_server_uuid = srv["id"]
                    return self._cf_server_uuid
        raise RuntimeError(f"CF virtual server '{self.cf_server_name}' not found")

    async def _invoke_mcpjungle(self, full_name: str, params: dict) -> tuple[dict, int]:
        body = {"name": full_name, **params}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.gateway_url}/api/v0/tools/invoke",
                json=body,
                timeout=self.timeout,
            )
        return resp.json(), resp.status_code

    async def _invoke_cf(self, full_name: str, params: dict) -> tuple[dict, int]:
        cf_tool_name = full_name.replace("__", "-").replace("_", "-")
        server_uuid = await self._get_cf_server_uuid()
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": cf_tool_name, "arguments": params},
        }
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.gateway_url}/servers/{server_uuid}/mcp",
                headers={
                    "Authorization": f"Bearer {self._cf_jwt()}",
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                },
                json=payload,
                timeout=self.timeout,
            )
        data = resp.json()
        result = data.get("result", {})
        content = result.get("content", [])
        return {"content": content}, resp.status_code

    async def _invoke_direct(self, full_name: str, params: dict) -> tuple[dict, int]:
        if self.gateway_backend == "contextforge":
            return await self._invoke_cf(full_name, params)
        return await self._invoke_mcpjungle(full_name, params)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def _fetch_skill(self, skill_id: str) -> dict:
        """Fetch skill from governance; raises ToolAccessDenied if revoked or missing."""
        token = await self._get_token()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.governance_url}/skills/{skill_id}",
                headers=headers,
                timeout=10.0,
            )
        if resp.status_code == 410:
            raise ToolAccessDenied(f"skill {skill_id!r} is revoked")
        if resp.status_code == 404:
            raise ToolAccessDenied(f"skill {skill_id!r} not found")
        resp.raise_for_status()
        return resp.json()

    async def _run_rollback(self, rollback_steps: list, inputs: dict) -> None:
        for rs in rollback_steps:
            action = rs.get("action") if isinstance(rs, dict) else rs
            try:
                await self.call_tool(action, inputs)
            except Exception as e:
                logger.warning("rollback step %s failed: %s", action, e)

    async def _execute_step(self, step: dict, inputs: dict) -> dict:
        """Invoke one skill step; raises ToolAccessDenied on denial or signal mismatch."""
        action = step.get("action") or step.get("tool", "")
        result = await self.call_tool(action, inputs)
        expected = step.get("expected_signal")
        if expected and isinstance(result, dict):
            if not all(k in result for k in expected):
                raise ToolAccessDenied(f"signal mismatch on step {action!r}: expected keys {list(expected)}")
        return {"step": action, "result": result}

    def _parse_steps(self, skill: dict) -> list:
        steps = skill["steps"]
        return json.loads(steps) if isinstance(steps, str) else steps

    @staticmethod
    def _count_completed(results: list) -> int:
        return sum(1 for r in results if not r.get("skipped"))

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

    async def execute_skill(self, skill_id: str, inputs: dict | None = None) -> dict:
        """Execute a promoted skill step-by-step with per-step OPA re-check."""
        if not self.governance_url:
            raise RuntimeError("execute_skill requires governance_url to be set")
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
        return {"skill_id": skill_id, "steps_completed": self._count_completed(results), "results": results}

    async def call_tool(self, tool_name: str, params: dict) -> dict:
        full_name = self._resolve_name(tool_name)

        if self.governance_url:
            token = await self._get_token()
            await self._governance_check(token, full_name)

            start = int(time.time() * 1000)
            data, status = await self._invoke_direct(full_name, params)
            latency = int(time.time() * 1000) - start

            self.last_calls.append({"tool": tool_name, "status": status})
            logger.info("tool_call tool=%s status=%d backend=%s", tool_name, status, self.gateway_backend)

            req_hash = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
            resp_hash = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]
            asyncio.create_task(self._governance_audit(token, full_name, req_hash, resp_hash, latency))

            return self._unwrap(data, status, tool_name)

        # Legacy mode: gateway_url is the governance proxy
        headers = await self._auth_headers()
        resp = await self._post(tool_name, full_name, params, headers)
        return self._unwrap(resp.json(), resp.status_code, tool_name)
