import logging
import re
import subprocess

from ..base import Checker
from ..models import Violation

logger = logging.getLogger(__name__)

# Xenon outputs lines like:
#   path/to/file.py:42:0: M: myproject.foo.MyClass.my_method - F
# where the trailing letter is the rank (A-F).

_LINE_RE = re.compile(
    r"^(.+?):(\d+):\d+:\s+\w:\s+.*?\s+-\s+([A-F])\s*$",
    re.MULTILINE,
)

# Ranks above this threshold are violations
_MAX_ALLOWED_RANK = "B"


def _rank_to_int(rank: str) -> int:
    return "ABCDEF".index(rank)


def _run_xenon(repo_path: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            [
                "xenon",
                "--max-absolute", _MAX_ALLOWED_RANK,
                "--max-modules", "A",
                "--max-average", "A",
                repo_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        logger.warning("xenon not installed — skipping")
        return None
    except subprocess.TimeoutExpired:
        logger.warning("xenon timed out for %s", repo_path)
        return None


def _parse_xenon_output(output: str) -> list[Violation]:
    violations = []
    for match in _LINE_RE.finditer(output):
        filepath = match.group(1)
        line = int(match.group(2))
        rank = match.group(3)
        if _rank_to_int(rank) > _rank_to_int(_MAX_ALLOWED_RANK):
            violations.append(Violation(
                rule="complexity-limit",
                severity="SOFT",
                file=f"{filepath}:{line}",
                message=f"Cyclomatic complexity rank {rank} exceeds allowed max {_MAX_ALLOWED_RANK}",
            ))
    return violations


class XenonChecker(Checker):
    """Run xenon on the repo and map complexity violations to SOFT violations."""

    async def check(self, repo_path: str) -> list[Violation]:
        result = _run_xenon(repo_path)
        if result is None or result.returncode == 0:
            return []
        return _parse_xenon_output(result.stdout or result.stderr or "")
