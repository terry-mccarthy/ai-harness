"""Unit tests for github_mcp/server.py — repo_conventions_read tool."""
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# Loaded under a unique sys.modules key rather than a bare `import server` —
# multiple services ship a same-named server.py, so a bare import would
# collide with (or be shadowed by) whichever other service's server module
# another test file already cached under sys.modules["server"] in this
# pytest session. See test_metrics_endpoints.py for the same pattern.
_MODULE_NAME = "_github_mcp_server_under_test"
_SERVER_PATH = Path(__file__).resolve().parents[2] / "services" / "github_mcp" / "server.py"


def _load_github_mcp_server():
    if _MODULE_NAME in sys.modules:
        return sys.modules[_MODULE_NAME]
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, _SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


_github_mcp_server = _load_github_mcp_server()
repo_conventions_read = _github_mcp_server.repo_conventions_read

SAMPLE_CONTRIBUTING = """# Contributing

## Code Style
Use Black with 100 char line length.

## Tests
Always write tests for new features.
"""


def _mock_sync_get(status: int = 200, text: str = ""):
    """Mock httpx.get (sync) used by _default_branch."""
    from unittest.mock import MagicMock
    mock_resp = MagicMock()
    mock_resp.status_code = status
    mock_resp.json.return_value = {"default_branch": "main"}
    return mock_resp


@pytest.fixture
def mock_httpx():
    """Patch both sync httpx.get and async httpx.AsyncClient.get."""
    def _make(
        async_files: list[tuple[int, str]] | None = None,
        sync_status: int = 200,
    ):
        if async_files is None:
            async_files = [(200, SAMPLE_CONTRIBUTING), (404, ""), (404, ""), (404, "")]

        patchers = []

        # Mock sync httpx.get (used by _default_branch)
        sync_resp = _mock_sync_get(status=sync_status)
        sync_patcher = patch.object(_github_mcp_server.httpx, "get", return_value=sync_resp)
        sync_patcher.start()
        patchers.append(sync_patcher)

        # Mock async httpx.AsyncClient (used for file fetching)
        async_mocks = []
        for status, text in async_files:
            m = AsyncMock()
            m.status_code = status
            m.text = text
            async_mocks.append(m)

        async_patcher = patch.object(_github_mcp_server.httpx, "AsyncClient")
        mock_cls = async_patcher.start()
        mock_client = mock_cls.return_value.__aenter__.return_value
        mock_client.get.side_effect = async_mocks
        patchers.append(async_patcher)

        def cleanup():
            for p in patchers:
                p.stop()
        return cleanup
    return _make


@pytest.mark.asyncio
async def test_repo_conventions_read_returns_contributing(mock_httpx):
    cleanup = mock_httpx()
    try:
        result = await repo_conventions_read(repo="https://github.com/owner/repo")
    finally:
        cleanup()

    assert result["repo"] == "owner/repo"
    assert len(result["conventions"]) > 0
    assert result["conventions"][0]["path"] == "CONTRIBUTING.md"
    assert "Black" in result["conventions"][0]["content"]


@pytest.mark.asyncio
async def test_repo_conventions_read_empty_repo(mock_httpx):
    all_404 = [(404, ""), (404, ""), (404, ""), (404, "")]
    cleanup = mock_httpx(async_files=all_404)
    try:
        result = await repo_conventions_read(repo="https://github.com/owner/repo")
    finally:
        cleanup()

    assert result["conventions"] == []
    assert "no conventions files found" in result.get("message", "")


@pytest.mark.asyncio
async def test_repo_conventions_read_query_filtering(mock_httpx):
    cleanup = mock_httpx()
    try:
        result = await repo_conventions_read(
            repo="https://github.com/owner/repo",
            query="Tests",
        )
    finally:
        cleanup()

    assert len(result["conventions"]) == 1
    assert "Tests" in result["conventions"][0]["content"]


@pytest.mark.asyncio
async def test_repo_conventions_read_partial_404(mock_httpx):
    contrib_ok = (200, SAMPLE_CONTRIBUTING)
    rest_404 = (404, "")
    async_files = [contrib_ok, rest_404, rest_404, rest_404]
    cleanup = mock_httpx(async_files=async_files)
    try:
        result = await repo_conventions_read(repo="https://github.com/owner/repo")
    finally:
        cleanup()

    assert len(result["conventions"]) == 1
    assert result["conventions"][0]["path"] == "CONTRIBUTING.md"
