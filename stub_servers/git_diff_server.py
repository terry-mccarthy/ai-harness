import logging
import os
import subprocess
import urllib.request
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import uvicorn

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "git_diff_stub",
    host="0.0.0.0",
    port=9001,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

_GITHUB_API = "https://api.github.com/repos"


def _fetch_github_pr_diff(github_repo: str, pr_number: int, token: str | None) -> str:
    """Fetch a pull request unified diff from the GitHub API."""
    url = f"{_GITHUB_API}/{github_repo}/pulls/{pr_number}"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github.v3.diff",
            "User-Agent": "ai-harness-git-diff-stub",
        },
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req) as resp:
        return resp.read().decode()


@mcp.tool()
def git_diff(
    repo_path: str = "/app/sample-repo",
    base: str = "HEAD~1",
    head: str = "HEAD",
    diff_text: str = "",
    pr_number: int | None = None,
    github_repo: str | None = None,
) -> dict:
    """Return a unified diff from one of three sources:
    - diff_text: pre-computed diff (highest priority — always used if non-empty)
    - pr_number + github_repo: fetch from GitHub API
    - repo_path + base + head: run git diff inside the container
    """
    if diff_text:
        return {"diff": diff_text, "source": "passthrough"}

    if pr_number is not None:
        if not github_repo:
            raise ValueError("github_repo is required when pr_number is provided")
        token = os.environ.get("GITHUB_TOKEN")
        diff = _fetch_github_pr_diff(github_repo, pr_number, token=token)
        return {"diff": diff, "source": "github"}

    result = subprocess.run(
        ["git", "diff", f"{base}..{head}"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"git diff failed: {result.stderr.strip()}")
    return {"diff": result.stdout, "source": "git"}


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9001)
