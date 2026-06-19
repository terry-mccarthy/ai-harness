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
