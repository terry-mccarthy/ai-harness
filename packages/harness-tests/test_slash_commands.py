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


def test_commands_reference_valid_tools():
    failures = []
    for filename in REQUIRED_COMMANDS:
        path = COMMANDS_DIR / filename
        content = path.read_text()
        if filename == "sync-skills.md":
            if "sync-skills" not in content and "sync_skills" not in content:
                failures.append(f"{filename}: missing sync-skills reference")
        else:
            referenced = [t for t in REGISTRY_TOOLS if t in content]
            if not referenced:
                failures.append(f"{filename}: no registry__* tool referenced")
    assert not failures, "\n".join(failures)
