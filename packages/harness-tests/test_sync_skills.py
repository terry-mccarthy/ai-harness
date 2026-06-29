"""sync_skills.py — integration tests (subprocess seam).

Tests:
- Script writes skill-<name>.md for each active skill
- Generated file has correct frontmatter (skill_id, version, agent_role, expires_at)
- Generated file has correct body (description, instructions, task_patterns as bullets)
- Stale skill-*.md files are deleted when skill is no longer active
- Stdout contains "synced N skills, removed M stale commands"
"""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090")
SCRIPT_PATH = Path(__file__).parent.parent.parent / "scripts" / "sync_skills.py"

pytestmark = pytest.mark.integration

_SKILL_PAYLOAD = {
    "name": "test-sync-skill",
    "agent_role": "sre",
    "description": "A skill for testing sync",
    "prompt_template": "You are an SRE. Do the thing.",
    "steps": [{"action": "observability_query", "params": {}, "on_failure": "ABORT"}],
    "preconditions": {
        "env_constraints": {},
        "task_patterns": ["high latency", "db spike"],
    },
    "input_schema": {"type": "object"},
    "output_contract": {"type": "object"},
}


def _get_token(client_id: str = "human-operator", secret: str = "human-operator-secret") -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": secret},
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _author_skill(payload: dict | None = None) -> str:
    token = _get_token()
    body = {**_SKILL_PAYLOAD, **(payload or {})}
    resp = httpx.post(
        f"{GOVERNANCE_URL}/skills/author",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )
    assert resp.status_code == 201
    return resp.json()["skill_id"]


def _revoke_skill(skill_id: str) -> None:
    token = _get_token()
    httpx.post(
        f"{GOVERNANCE_URL}/skills/{skill_id}/revoke",
        json={"reason": "test teardown"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
    )


def _run_sync(tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GOVERNANCE_URL": GOVERNANCE_URL,
        "HUMAN_OPERATOR_SECRET": os.environ.get("HUMAN_OPERATOR_SECRET", "human-operator-secret"),
        "COMMANDS_DIR": str(tmp_path),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Tracer bullet: script creates command files for active skills
# ---------------------------------------------------------------------------


def test_sync_creates_command_files(tmp_path):
    skill_id = _author_skill({"name": "sync-tracer-bullet"})
    try:
        result = _run_sync(tmp_path)
        assert result.returncode == 0, result.stderr
        files = list(tmp_path.glob("skill-*.md"))
        names = [f.stem for f in files]
        assert any("sync-tracer-bullet" in n for n in names), f"expected skill file, got: {names}"
    finally:
        _revoke_skill(skill_id)


# ---------------------------------------------------------------------------
# Generated file has correct frontmatter
# ---------------------------------------------------------------------------


def test_sync_command_frontmatter(tmp_path):
    import yaml

    skill_id = _author_skill({"name": "sync-frontmatter-test"})
    try:
        _run_sync(tmp_path)
        skill_file = tmp_path / "skill-sync-frontmatter-test.md"
        assert skill_file.exists(), f"{skill_file} not created"
        content = skill_file.read_text()
        # YAML frontmatter is between --- delimiters
        parts = content.split("---")
        assert len(parts) >= 3, "missing YAML frontmatter delimiters"
        fm = yaml.safe_load(parts[1])
        assert fm["skill_id"] == skill_id
        assert fm["version"] == 1
        assert fm["agent_role"] == "sre"
        assert "expires_at" in fm
    finally:
        _revoke_skill(skill_id)


# ---------------------------------------------------------------------------
# Generated file has correct body
# ---------------------------------------------------------------------------


def test_sync_command_body(tmp_path):
    skill_id = _author_skill({"name": "sync-body-test"})
    try:
        _run_sync(tmp_path)
        skill_file = tmp_path / "skill-sync-body-test.md"
        content = skill_file.read_text()
        assert "A skill for testing sync" in content
        assert skill_id in content
        assert "registry__get_skill_prompt" in content
        assert "registry__execute_skill" in content
        # task_patterns rendered as bullet list
        assert "- high latency" in content
        assert "- db spike" in content
    finally:
        _revoke_skill(skill_id)


# ---------------------------------------------------------------------------
# Stale files are removed on next sync
# ---------------------------------------------------------------------------


def test_sync_removes_stale_commands(tmp_path):
    import yaml

    skill_id = _author_skill({"name": "sync-stale-test"})
    # First sync — creates the file
    _run_sync(tmp_path)
    stale_file = tmp_path / "skill-sync-stale-test.md"
    assert stale_file.exists()

    # Revoke the skill, then sync again
    _revoke_skill(skill_id)
    result = _run_sync(tmp_path)
    assert result.returncode == 0, result.stderr
    assert not stale_file.exists(), "stale file should have been removed"


# ---------------------------------------------------------------------------
# Stdout summary
# ---------------------------------------------------------------------------


def test_sync_prints_summary(tmp_path):
    skill_id = _author_skill({"name": "sync-summary-test"})
    try:
        result = _run_sync(tmp_path)
        assert result.returncode == 0, result.stderr
        assert "synced" in result.stdout
        assert "removed" in result.stdout
    finally:
        _revoke_skill(skill_id)
