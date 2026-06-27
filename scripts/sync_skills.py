"""Sync active skills from the registry to .claude/commands/skill-*.md."""

import json
import os
from pathlib import Path

import httpx
import yaml

GOVERNANCE_URL = os.environ.get("GOVERNANCE_URL", "http://localhost:8090").rstrip("/")
HUMAN_OPERATOR_SECRET = os.environ.get("HUMAN_OPERATOR_SECRET", "human-operator-secret")
COMMANDS_DIR = Path(os.environ.get("COMMANDS_DIR", ".claude/commands"))

_TEMPLATE = """\
---
skill_id: {skill_id}
version: {version}
agent_role: {agent_role}
expires_at: {expires_at}
---

# {name}

{description}

## Instructions

Call `registry__get_skill_prompt` with `skill_id="{skill_id}"` to load the full prompt
for this skill, then apply those instructions to the current task.

If the task requires tool calls, use `registry__execute_skill` with
`skill_id="{skill_id}"` and `inputs=$ARGUMENTS`.

## When to use

{task_patterns}"""


def _get_token() -> str:
    resp = httpx.post(
        f"{GOVERNANCE_URL}/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "human-operator",
            "client_secret": HUMAN_OPERATOR_SECRET,
        },
        timeout=10.0,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _list_active_skills(token: str) -> list[dict]:
    resp = httpx.get(
        f"{GOVERNANCE_URL}/skills",
        headers={"Authorization": f"Bearer {token}"},
        params={"status": "active"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def _render_task_patterns(patterns: list[str]) -> str:
    if not patterns:
        return "_No specific task patterns defined._"
    return "\n".join(f"- {p}" for p in patterns)


def _skill_filename(name: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name).strip("-")
    return f"skill-{safe}.md"


def _read_frontmatter_skill_id(path: Path) -> str | None:
    try:
        content = path.read_text()
        parts = content.split("---")
        if len(parts) < 3:
            return None
        fm = yaml.safe_load(parts[1])
        return fm.get("skill_id")
    except Exception:
        return None


def main() -> None:
    token = _get_token()
    active_skills = _list_active_skills(token)
    active_ids = {s["id"] for s in active_skills}

    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)

    # Remove stale skill-*.md files
    removed = 0
    for existing in COMMANDS_DIR.glob("skill-*.md"):
        fid = _read_frontmatter_skill_id(existing)
        if fid and fid not in active_ids:
            existing.unlink()
            removed += 1

    # Write/update active skill files
    synced = 0
    for skill in active_skills:
        preconditions = skill.get("preconditions")
        if isinstance(preconditions, str):
            preconditions = json.loads(preconditions) if preconditions else {}
        if not preconditions:
            preconditions = {}
        task_patterns = preconditions.get("task_patterns", [])

        content = _TEMPLATE.format(
            skill_id=skill["id"],
            version=skill.get("version", 1),
            agent_role=skill.get("agent_role", ""),
            expires_at=skill.get("expires_at", ""),
            name=skill["name"],
            description=skill.get("description") or "_No description provided._",
            task_patterns=_render_task_patterns(task_patterns),
        )
        out = COMMANDS_DIR / _skill_filename(skill["name"])
        out.write_text(content)
        synced += 1

    print(f"synced {synced} skills, removed {removed} stale commands")


if __name__ == "__main__":
    main()
