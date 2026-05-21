import httpx
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Maps short tool names to MCPJungle's server__tool format
TOOL_NAME_MAP = {
    "git_diff": "git_diff_stub__git_diff",
    "run_linter": "linter_stub__run_linter",
    "review_diff": "review_server__review_diff",
}


class ToolAccessDenied(Exception):
    pass


@dataclass
class GatewayClient:
    """Thin wrapper: call tools through MCPJungle /api/v0/tools/invoke."""
    gateway_url: str
    client_id: str       # kept for interface compatibility
    client_secret: str   # kept for interface compatibility
    last_calls: list = field(default_factory=list, repr=False)

    async def call_tool(self, tool_name: str, params: dict) -> dict:
        full_name = TOOL_NAME_MAP.get(tool_name)
        if full_name is None:
            raise ToolAccessDenied(f"403 Forbidden: {tool_name} not in allowed tool list")

        body = {"name": full_name, **params}
        logger.debug("tool_call request: %s", body)

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.gateway_url}/api/v0/tools/invoke",
                json=body,
                timeout=30.0,
            )

        self.last_calls.append({"tool": tool_name, "status": resp.status_code})
        logger.info("tool_call tool=%s status=%d", tool_name, resp.status_code)

        if resp.status_code == 403:
            raise ToolAccessDenied(f"403 Forbidden: {tool_name}")

        resp.raise_for_status()
        data = resp.json()
        logger.debug("tool_call raw response: %s", data)

        # MCPJungle returns {"content": [...]} — unwrap first text item to parsed JSON
        import json as _json
        items = data.get("content") or data.get("result") or []
        if items and isinstance(items[0], dict) and items[0].get("type") == "text":
            try:
                return _json.loads(items[0]["text"])
            except _json.JSONDecodeError:
                return items[0]["text"]
        return data
