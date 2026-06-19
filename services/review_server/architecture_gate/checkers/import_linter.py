import json
import logging
import subprocess
from pathlib import Path

from ..base import Checker
from ..models import Violation

logger = logging.getLogger(__name__)

_CONFIG_TEMPLATE = """[importlinter]
root_package = {root_pkg}

[[tool.importlinter.contracts]]
name = "Layer enforcement"
type = "layers"
layers = [
    "infrastructure",
    "application",
    "domain",
]
"""


class ImportLinterChecker(Checker):
    """Run import-linter on the repo and map layer violations to HARD violations."""

    async def check(self, repo_path: str) -> list[Violation]:
        try:
            result = subprocess.run(
                ["import-linter", "--format", "json"],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError:
            logger.warning("import-linter not installed — skipping")
            return []
        except subprocess.TimeoutExpired:
            logger.warning("import-linter timed out for %s", repo_path)
            return []

        if result.returncode == 0:
            return []

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []

        violations = []
        for entry in data.get("contracts", []):
            for violation in entry.get("violations", []):
                violations.append(Violation(
                    rule=violation.get("rule", "layer-violation"),
                    severity="HARD",
                    file=violation.get("module", ""),
                    message=violation.get("message", "Illegal import"),
                ))
        return violations
