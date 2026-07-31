"""GET/PUT /config HTTP routes for review_server.

Carved out of server.py (issue #09): the runtime-config read/write endpoint
handlers, built on top of the config infra extracted in issue #06
(``core/config.py``'s ``_CONFIG``, ``_check_api_key``, ``_sanitize_cfg``,
``_env_cfg``, ``_save_config_to_pg``). The state and persistence helpers stay
in ``core/config.py``; this module only owns the two HTTP route handlers and
their small request-parsing helpers.

Unlike issues #07/#08 (services/code_analysis.py, services/architecture_gate.py),
there is no GatewayClient/LLM-provider construction behind these routes, so
the DI-seam gotcha those extractions worked around (tests patching
``review_server.GatewayClient`` / ``review_server._build_llm_provider``) does
not apply here. packages/harness-tests/test_review_http.py's config tests
instead patch/read ``review_server._CONFIG`` directly. Since issue #06
already made ``_CONFIG`` a single dict object living in ``core.config``
(server.py just imports the same reference, it doesn't copy it), mutating it
via ``core.config._CONFIG``, ``review_server._CONFIG``, or this module's
``_CONFIG`` all mutate the same underlying dict — no aliasing issue from this
extraction.

Mounting: FastMCP's ``mcp.custom_route(path, methods=...)`` is a decorator
that needs the live ``FastMCP`` instance to append routes onto
(``mcp._custom_starlette_routes``) — it isn't a standalone ``APIRouter``
object you build in isolation and mount later via ``app.include_router()``
the way governance's ``routers/policy.py`` etc. do with FastAPI. So instead
of module-level ``@mcp.custom_route(...)``-decorated functions (which would
need a module-level ``mcp`` to decorate onto, recreating the very coupling
this extraction is meant to remove), this module exposes
``register_config_routes(mcp)``: a plain function that takes the FastMCP
instance and defines/decorates the route handlers as closures inside it.
server.py calls ``register_config_routes(mcp)`` once, at the same place the
inline ``@mcp.custom_route("/config", ...)`` definitions used to live.
"""
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from review_server_core.config import (
    _CONFIG,
    _check_api_key,
    _env_cfg,
    _sanitize_cfg,
    _save_config_to_pg,
)


async def _parse_json_body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    return body


def _update_provider_config(prov: str, overrides: Any) -> None:
    if not isinstance(overrides, dict):
        return
    _CONFIG.setdefault(prov, {})
    for k, v in overrides.items():
        if v is None:
            _CONFIG[prov].pop(k, None)
        else:
            _CONFIG[prov][k] = v


def register_config_routes(mcp) -> None:
    """Register GET/PUT /config on the given FastMCP instance."""

    @mcp.custom_route("/config", methods=["GET"])
    async def get_config(request: Request) -> JSONResponse:
        """Return effective runtime config (env vars + overrides, secrets masked)."""
        if not _check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_sanitize_cfg(_env_cfg()))

    @mcp.custom_route("/config", methods=["PUT"])
    async def put_config(request: Request) -> JSONResponse:
        if not _check_api_key(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        body = await _parse_json_body(request)
        if body is None:
            return JSONResponse({"error": "invalid JSON body"}, status_code=422)

        if "llm_provider" in body:
            _CONFIG["llm_provider"] = body["llm_provider"]

        for prov in ("ollama", "gemini", "openrouter"):
            _update_provider_config(prov, body.get(prov))

        await _save_config_to_pg()

        return JSONResponse({"status": "ok", "config": _sanitize_cfg(_CONFIG)})
