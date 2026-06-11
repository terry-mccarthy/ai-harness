import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

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

_RULES_FILE = Path(__file__).parent / "semgrep-rules.yml"
_SEVERITY_MAP = {"ERROR": "CRITICAL", "WARNING": "WARNING", "INFO": "INFO"}


def _parse_diff(diff_text: str) -> dict[str, str]:
    """Extract added lines per file from a unified diff.

    Returns {relative_path: added_content}. Only files that have at least one
    added line are included so we don't create empty temp files for semgrep.
    """
    files: dict[str, list[str]] = {}
    current_file: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            files.setdefault(current_file, [])
        elif current_file and line.startswith("+") and not line.startswith("+++"):
            files[current_file].append(line[1:])
    return {path: "\n".join(lines) for path, lines in files.items() if lines}


def _semgrep_findings(tmpdir: str) -> list[dict]:
    """Run semgrep on tmpdir and return a list of normalised finding dicts."""
    result = subprocess.run(
        [
            "semgrep", "scan",
            "--config", str(_RULES_FILE),
            "--json", "--quiet",
            "--no-autofix",
            tmpdir,
        ],
        capture_output=True,
        text=True,
    )
    # semgrep exits 0 for no findings, 1 for findings found, 2+ for errors
    if result.returncode not in (0, 1):
        logger.warning("semgrep exited %d: %s", result.returncode, result.stderr[:200])
        return []

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        logger.warning("semgrep output not valid JSON: %s", result.stdout[:200])
        return []

    findings = []
    for hit in data.get("results", []):
        sev = hit.get("extra", {}).get("severity", "WARNING")
        findings.append({
            "rule": hit.get("check_id", "unknown"),
            "message": hit.get("extra", {}).get("message", ""),
            "severity": _SEVERITY_MAP.get(sev, "WARNING"),
            "path": Path(hit.get("path", "")).name,
            "line": hit.get("start", {}).get("line"),
        })
    return findings


@mcp.tool()
def run_linter(diff_text: str) -> dict:
    """Run semgrep against the added lines in a diff; return structured findings."""
    file_contents = _parse_diff(diff_text)
    if not file_contents:
        return {"warnings": [], "error_count": 0}

    with tempfile.TemporaryDirectory() as tmpdir:
        for filepath, content in file_contents.items():
            dest = Path(tmpdir) / Path(filepath).name
            dest.write_text(content)

        warnings = _semgrep_findings(tmpdir)

    error_count = sum(1 for w in warnings if w["severity"] == "CRITICAL")
    return {"warnings": warnings, "error_count": error_count}


if __name__ == "__main__":
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=9002)
