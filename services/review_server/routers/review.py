"""run_skill / code-forensics-style / architecture-gate-check FastMCP tools,
plus the /metrics HTTP route, for review_server.

Carved out of server.py (issue #10, the final slice of the review_server
decomposition started in issues #06-#09). This module holds the tools/routes
that have no GatewayClient/LLM-provider dependency-injection seam for
unittest.mock.patch.object(review_server, ...) to hook into — see the
"DI-seam-blocked" comment block in server.py for the functions that
deliberately stay behind and why.

Verified via grep across packages/harness-tests/ before moving anything: no
test patches ``review_server.run_skill``, ``review_server.execute_architecture_check``,
``review_server.code_health_score``, ``review_server.codebase_hotspots``,
``review_server.logical_coupling``, or ``review_server.metrics_route`` via
``patch.object``, and none calls any of them directly as
``review_server.<name>(...)``/``_srv.<name>(...)``. Tests that reference
``codebase_hotspots``/``execute_architecture_check`` by string (e.g.
test_phase7_aac.py, test_unit_adversarial_architecture_critic.py) are mocking
those as *remote MCP tool names* on a fake ``GatewayClient``, not patching
this module's attributes — moving the real implementations here doesn't
affect them.

Mounting: same reasoning as issue #09's routers/config.py — FastMCP's
``@mcp.tool()``/``@mcp.custom_route(...)`` decorators need the live
``FastMCP`` instance to register onto, so this module exposes
``register_review_routes(mcp)``, a plain function that defines the
tools/route as closures inside it. server.py calls
``register_review_routes(mcp)`` once, alongside ``register_config_routes(mcp)``.
"""
import logging
import os

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import Response

from harness_gateway.client import GatewayClient
from metrics import REGISTRY
from services.architecture_gate import _run_execute_architecture_check


def register_review_routes(mcp) -> None:
    """Register run_skill, execute_architecture_check, code_health_score,
    codebase_hotspots, logical_coupling, and GET /metrics on the given
    FastMCP instance.
    """

    @mcp.tool()
    async def run_skill(
        skill_id: str,
        inputs: dict | None = None,
    ) -> dict:
        """Execute a promoted governed skill by ID, running each step through OPA.

        Args:
            skill_id: The skill identifier (e.g. ``"sre:triage-incident"``).
            inputs: Optional input parameters passed to each step.
        """
        gateway = GatewayClient(
            gateway_url=os.environ["MCPJUNGLE_URL"],
            governance_url=os.environ.get("GOVERNANCE_URL"),
            client_id=os.environ.get("SKILL_CLIENT_ID", "sre"),
            client_secret=os.environ.get("SKILL_CLIENT_SECRET", os.environ.get("SRE_SECRET", "")),
        )
        try:
            return await gateway.execute_skill(skill_id, inputs)
        except Exception as e:
            logging.exception("run_skill failed for %s", skill_id)
            raise RuntimeError(str(e)) from e

    @mcp.custom_route("/metrics", methods=["GET"])
    async def metrics_route(request: Request) -> Response:
        """Prometheus metrics endpoint, scraped by the monitoring stack."""
        return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)

    @mcp.tool()
    async def execute_architecture_check(
        target_language: str,
        repo_path: str,
    ) -> dict:
        """Execute static analysis checks on the target codebase and return a GateSignalContract.

        Args:
            target_language: The programming language of the codebase (e.g., ``'python'``, ``'php'``, ``'typescript'``).
            repo_path: The directory path or GitHub URL of the codebase to analyze.
        """
        return await _run_execute_architecture_check(target_language, repo_path)

    @mcp.tool()
    async def code_health_score(
        file_paths: list[str],
        repo: str,
    ) -> list[dict]:
        """Analyze code health (complexity, function length) for specific files in a GitHub repo.

        Fetches each file from the GitHub API and runs radon cyclomatic
        complexity analysis. Returns scores 0-10 (higher = healthier),
        sorted worst-first.

        Args:
            file_paths: List of file paths relative to the repo root (e.g. ``["src/main.py", "lib/utils.ts"]``).
            repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
        """
        from code_analysis import get_code_health as _get_code_health

        try:
            return await _get_code_health(file_paths, repo)
        except Exception as e:
            logging.exception("code_health_score failed")
            return [{"error": str(e)}]

    @mcp.tool()
    async def codebase_hotspots(
        repo: str,
        top_n: int = 10,
        language: str | None = None,
    ) -> list[dict]:
        """Rank files in a GitHub repo by complexity-based hotspot risk.

        Fetches the file tree from the GitHub API, downloads source files,
        and ranks them by cyclomatic complexity. High-complexity files
        are hotspots most likely to contain bugs.

        Args:
            repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
            top_n: Number of top hotspots to return (default 10).
            language: Optional language filter (e.g. ``"python"``, ``"typescript"``).
        """
        from code_analysis import get_hotspots as _get_hotspots

        try:
            return await _get_hotspots(repo, top_n=top_n, language=language)
        except Exception as e:
            logging.exception("codebase_hotspots failed")
            return [{"error": str(e)}]

    @mcp.tool()
    async def logical_coupling(
        repo: str,
        file_path: str,
        max_commits: int = 50,
    ) -> list[dict]:
        """Find files that historically change together with a given file.

        Uses the GitHub commits API to find recent commits touching
        ``file_path``, then extracts all other files changed in those
        commits. Returns the co-changing files ranked by frequency.

        Args:
            repo: GitHub URL (e.g. ``"https://github.com/owner/repo"``).
            file_path: Path to the file to analyse (e.g. ``"src/main.py"``).
            max_commits: Maximum recent commits to inspect (default 50).
        """
        from code_analysis import get_logical_coupling as _get_logical_coupling

        try:
            return await _get_logical_coupling(repo, file_path, max_commits=max_commits)
        except Exception as e:
            logging.exception("logical_coupling failed")
            return [{"error": str(e)}]
