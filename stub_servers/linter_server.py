import logging
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import uvicorn

logging.getLogger().setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
logger = logging.getLogger(__name__)

mcp = FastMCP(
    "linter_stub",
    host="0.0.0.0",
    port=9002,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)

@mcp.tool()
def run_linter(diff_text: str) -> dict:
    """Return fake lint warnings based on naive pattern matching."""
    added = "\n".join(
        line[1:] for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    warnings = []
    if "print(" in added:
        warnings.append({"rule": "no-print", "message": "print() found — possible secret leak", "severity": "WARNING"})
    if "password" in added.lower() and "print" in added.lower():
        warnings.append({"rule": "secret-in-log", "message": "Password may be logged", "severity": "CRITICAL"})
    return {"warnings": warnings, "error_count": sum(1 for w in warnings if w["severity"] == "CRITICAL")}

if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9002)
