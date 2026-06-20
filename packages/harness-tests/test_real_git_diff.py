import pytest
from harness_gateway.client import GatewayClient

# The diff-proxy container has a baked-in sample repo at /app/sample-repo
# with one commit that adds a file containing a password print statement.
# These tests verify the tool runs real git commands against it.


@pytest.mark.integration
async def test_git_diff_returns_real_diff_format(gateway_client):
    """git_diff tool returns output that looks like an actual git diff."""
    result = await gateway_client.call_tool("git_diff", {
        "repo_path": "/app/sample-repo",
    })
    assert "diff --git" in result["diff"]
    assert result.get("source") == "git"


@pytest.mark.integration
async def test_git_diff_contains_commit_changes(gateway_client):
    """git_diff output contains the actual changed lines from the sample repo."""
    result = await gateway_client.call_tool("git_diff", {
        "repo_path": "/app/sample-repo",
    })
    diff = result["diff"]
    assert "password" in diff.lower() or "print" in diff.lower()


@pytest.mark.integration
async def test_git_diff_respects_ref(gateway_client):
    """git_diff accepts an optional base ref."""
    result = await gateway_client.call_tool("git_diff", {
        "repo_path": "/app/sample-repo",
        "base": "HEAD~1",
        "head": "HEAD",
    })
    assert "diff --git" in result["diff"]


@pytest.mark.integration
async def test_coverage_report_reachable(gateway_client):
    """coverage_report returns synthetic coverage data for given files."""
    result = await gateway_client.call_tool("coverage_report", {
        "file_paths": ["auth.py", "db.py"],
    })
    assert len(result["files"]) == 2
    assert result["files"][0]["path"] == "auth.py"
    assert result["overall_line_coverage"] == 85.0


@pytest.mark.integration
async def test_coverage_report_empty_paths(gateway_client):
    """coverage_report handles empty file list."""
    result = await gateway_client.call_tool("coverage_report", {
        "file_paths": [],
    })
    assert result["files"] == []


@pytest.mark.integration
async def test_repo_conventions_read_reachable(gateway_client):
    """repo_conventions_read returns conventions for a public repo."""
    result = await gateway_client.call_tool("repo_conventions_read", {
        "repo": "https://github.com/psf/black",
    })
    assert "repo" in result
    assert "conventions" in result


@pytest.mark.integration
async def test_repo_conventions_read_unknown_repo(gateway_client):
    """repo_conventions_read gracefully handles no conventions files."""
    result = await gateway_client.call_tool("repo_conventions_read", {
        "repo": "https://github.com/terry-mccarthy/ai-harness",
    })
    assert result["conventions"] == []
