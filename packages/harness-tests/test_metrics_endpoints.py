"""Unit tests for the /metrics Prometheus endpoint on the MCP stub/service servers.

No Docker stack needed — each server's ASGI app is exercised in-process via
httpx's ASGI transport, mirroring the pattern in test_review_http.py.
"""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.asyncio

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MODULES = {
    "linter_stub": _REPO_ROOT / "stub_servers" / "linter_server.py",
    "sre_stub": _REPO_ROOT / "stub_servers" / "sre_server.py",
    "diff_proxy": _REPO_ROOT / "stub_servers" / "diff_proxy_server.py",
    "github_mcp": _REPO_ROOT / "services" / "github_mcp" / "server.py",
    "skills_registry": _REPO_ROOT / "services" / "skills_registry" / "server.py",
}


def _load_module(alias: str, path: Path):
    """Load a server module by explicit path under a unique sys.modules key.

    Avoids collisions between github_mcp/server.py and skills_registry/server.py,
    which share the filename "server.py".
    """
    module_name = f"_metrics_test_{alias}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    with patch.dict("os.environ", {"ENV": "test"}, clear=False):
        spec = importlib.util.spec_from_file_location(module_name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = mod
        spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("alias", list(_MODULES))
async def test_metrics_endpoint_returns_prometheus_text(alias):
    """GET /metrics is reachable and returns the Prometheus text exposition format.

    Note: this doesn't assert on process_cpu_seconds_total/process_resident_memory_bytes
    specifically — prometheus_client's ProcessCollector reads /proc, which only exists on
    Linux, so those series are absent when this test runs on a macOS/non-Linux host even
    though they're correctly emitted inside the (Linux) Docker containers at runtime.
    """
    mod = _load_module(alias, _MODULES[alias])
    app = mod.mcp.streamable_http_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "python_info" in resp.text
