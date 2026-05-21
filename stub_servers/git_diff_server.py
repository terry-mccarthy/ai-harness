import logging
import os
import subprocess
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


@mcp.tool()
def git_diff(
    repo_path: str = "/app/sample-repo",
    base: str = "HEAD~1",
    head: str = "HEAD",
    diff_text: str = "",
) -> dict:
    """Run git diff on a repo and return the output. Falls back to echoing diff_text."""
    if diff_text:
        return {"diff": diff_text, "source": "passthrough"}

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
