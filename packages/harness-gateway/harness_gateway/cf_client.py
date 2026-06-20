"""ContextForge gateway client.

Translates the MCPJungle flat-invoke API surface to ContextForge's
JSON-RPC 2.0 tool-call API.  Used by Phase 5 tests and as the
alternative backend when GATEWAY_BACKEND=contextforge.

Registration flow (performed by the setup init container, not here):
  1. POST /gateways  with transport=STREAMABLEHTTP for each MCP stub
  2. POST /servers   to create a virtual server aggregating all tools
  The virtual server UUID is stored in CF_SERVER_UUID or discovered
  automatically from GET /servers by matching ``server_name``.

Tool-name mapping (MCPJungle → ContextForge):
  architect_stub__codebase_search  →  architect-stub-codebase-search
"""
import json
import logging
import time
import uuid as _uuid

import httpx
import jwt as pyjwt

logger = logging.getLogger(__name__)

# MCPJungle short-name → ContextForge tool slug
# Populated lazily; also derived by the name-translation function.
_TOOL_NAME_CACHE: dict[str, str] = {}


def _to_cf_tool_name(mcp_name: str) -> str:
    """Map MCPJungle format to ContextForge slug.

    architect_stub__codebase_search  →  architect-stub-codebase-search
    """
    return mcp_name.replace("__", "-").replace("_", "-")


def _parse_tool_result(result: dict) -> dict:
    content = result.get("content", [])
    if not (content and isinstance(content[0], dict) and content[0].get("type") == "text"):
        return result
    try:
        return json.loads(content[0]["text"])
    except json.JSONDecodeError:
        return {"text": content[0]["text"]}


class ContextForgeError(Exception):
    pass


class ContextForgeGatewayClient:
    """Synchronous gateway client that calls tools via ContextForge."""

    def __init__(
        self,
        cf_url: str,
        cf_jwt_secret: str,
        cf_admin_email: str = "admin@harness.local",
        server_name: str = "harness_all",
        server_uuid: str | None = None,
        timeout: float = 30.0,
    ):
        self.cf_url = cf_url.rstrip("/")
        self._secret = cf_jwt_secret
        self._admin_email = cf_admin_email
        self._server_name = server_name
        self._server_uuid = server_uuid  # can be pre-set from env var
        self._timeout = timeout

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _token(self) -> str:
        """Generate a short-lived ContextForge JWT."""
        now = int(time.time())
        payload = {
            "sub": self._admin_email,
            "preferred_username": "admin",
            "iat": now,
            "iss": "mcpgateway",
            "aud": "mcpgateway-api",
            "jti": str(_uuid.uuid4()),
            "exp": now + 3600,
        }
        return pyjwt.encode(payload, self._secret, algorithm="HS256")

    def _auth_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._token()}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

    # ------------------------------------------------------------------
    # Server discovery
    # ------------------------------------------------------------------

    def _discover_server_uuid(self) -> str:
        """Find the virtual server UUID by name via GET /servers."""
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.get(
                f"{self.cf_url}/servers",
                headers=self._auth_headers(),
            )
            resp.raise_for_status()
            for srv in resp.json():
                if srv.get("name") == self._server_name:
                    logger.debug("found CF server '%s' → %s", self._server_name, srv["id"])
                    return srv["id"]
        raise ContextForgeError(
            f"ContextForge virtual server '{self._server_name}' not found. "
            "Run the setup init container first."
        )

    def _get_server_uuid(self) -> str:
        if not self._server_uuid:
            self._server_uuid = self._discover_server_uuid()
        return self._server_uuid

    # ------------------------------------------------------------------
    # Tool invocation
    # ------------------------------------------------------------------

    def call_tool(self, tool_name: str, params: dict | None = None) -> dict:
        """Call a tool through ContextForge.

        ``tool_name`` may be in MCPJungle format (``server__tool``) or
        ContextForge slug (``server-tool``).  Both are accepted.
        """
        server_uuid = self._get_server_uuid()
        cf_tool_name = _to_cf_tool_name(tool_name)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": cf_tool_name,
                "arguments": params or {},
            },
        }
        with httpx.Client(timeout=self._timeout) as client:
            resp = client.post(
                f"{self.cf_url}/servers/{server_uuid}/mcp",
                headers=self._auth_headers(),
                json=payload,
            )
        logger.info("cf_call tool=%s status=%d", tool_name, resp.status_code)

        if resp.status_code == 404:
            raise ContextForgeError(f"Tool not found: {cf_tool_name}")
        resp.raise_for_status()

        data = resp.json()
        if "error" in data:
            raise ContextForgeError(f"ContextForge error: {data['error']}")

        result = data.get("result", {})
        content = result.get("content", [])
        return _parse_tool_result(result)
