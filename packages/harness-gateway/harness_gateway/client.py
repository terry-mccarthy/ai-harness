import json
import httpx
import logging
import time
from dataclasses import dataclass, field

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
    timeout: float = 180.0
    last_calls: list = field(default_factory=list, repr=False)
    _token: str | None = field(default=None, init=False, repr=False)
    _token_exp: float = field(default=0.0, init=False, repr=False)

    async def _get_token(self) -> str | None:
        """Fetch a bearer token if the gateway has an /oauth/token endpoint."""
        if not self.client_secret:
            return None
        if self._token and time.time() < self._token_exp - 30:
            return self._token
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    f"{self.gateway_url}/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                    timeout=10.0,
                )
            if resp.status_code == 404:
                return None  # no OAuth on this gateway (MCPJungle free tier direct)
            resp.raise_for_status()
            data = resp.json()
            self._token = data["access_token"]
            self._token_exp = time.time() + data.get("expires_in", 900)
            logger.debug(
                "fetched token for %s, exp in %ds",
                self.client_id,
                data.get("expires_in"),
            )
            return self._token
        except httpx.HTTPStatusError:
            raise
        except Exception as e:
            logger.warning("token fetch failed: %s", e)
            return None

    def _unwrap(self, resp: "httpx.Response", tool_name: str) -> dict:
        if resp.status_code == 403:
            raise ToolAccessDenied(f"403 Forbidden: {tool_name}")
        if resp.status_code == 401:
            raise ToolAccessDenied(f"401 Unauthorized: {tool_name}")
        resp.raise_for_status()
        data = resp.json()
        logger.debug("tool_call raw response: %s", data)
        items = data.get("content") or data.get("result") or []
        if items and isinstance(items[0], dict) and items[0].get("type") == "text":
            try:
                return json.loads(items[0]["text"])
            except json.JSONDecodeError:
                return items[0]["text"]
        return data

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
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _resolve_name(self, tool_name: str) -> str:
        full_name = TOOL_NAME_MAP.get(tool_name)
        if full_name is None:
            raise ToolAccessDenied(f"403 Forbidden: {tool_name} not in allowed tool list")
        return full_name

    async def call_tool(self, tool_name: str, params: dict) -> dict:
        full_name = self._resolve_name(tool_name)

        headers = await self._auth_headers()
        resp = await self._post(tool_name, full_name, params, headers)
        return self._unwrap(resp, tool_name)
