"""Management slash commands — unit tests (filesystem seam).

Tests:
- .claude/commands/skills-list.md exists
- All 10 management command files exist
- Each command references at least one valid registry__* tool or make sync-skills
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"

REQUIRED_COMMANDS = [
    "skills-list.md",
    "skill-view.md",
    "skill-create.md",
    "skill-label.md",
    "episodes-list.md",
    "skill-propose.md",
    "skill-promote.md",
    "skill-reject.md",
    "skill-revoke.md",
    "sync-skills.md",
]

# Short names from TOOL_NAME_MAP that are registry tools
REGISTRY_TOOLS = {
    "registry__list_skills",
    "registry__get_skill",
    "registry__get_skill_prompt",
    "registry__create_skill",
    "registry__list_episodes",
    "registry__get_episode",
    "registry__label_episode",
    "registry__list_candidates",
    "registry__get_candidate",
    "registry__propose_candidate",
    "registry__promote_candidate",
    "registry__reject_candidate",
    "registry__revoke_skill",
    "registry__execute_skill",
}


def test_skills_list_command_exists():
    assert (COMMANDS_DIR / "skills-list.md").exists()


def test_all_management_commands_exist():
    missing = [f for f in REQUIRED_COMMANDS if not (COMMANDS_DIR / f).exists()]
    assert not missing, f"missing command files: {missing}"


def _command_valid(filename: str, content: str) -> str | None:
    if filename == "sync-skills.md":
        if "sync-skills" not in content and "sync_skills" not in content:
            return f"{filename}: missing sync-skills reference"
        return None
    if not any(t in content for t in REGISTRY_TOOLS):
        return f"{filename}: no registry__* tool referenced"
    return None


def test_commands_reference_valid_tools():
    failures = [
        msg
        for filename in REQUIRED_COMMANDS
        if (msg := _command_valid(filename, (COMMANDS_DIR / filename).read_text()))
    ]
    assert not failures, "\n".join(failures)
