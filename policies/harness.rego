package harness

default allow = false

allow if {
    input.agent_role == "architect"
    input.tool_name in {"codebase_search", "adr_read", "architecture_review", "execute_architecture_check", "code_health_score", "codebase_hotspots", "logical_coupling", "issue_create"}
}

allow if {
    input.agent_role == "code_reviewer"
    input.tool_name in {"git_diff", "run_linter", "coverage_report", "repo_conventions_read", "review_diff", "architecture_review", "execute_architecture_check"}
}

allow if {
    input.agent_role == "sre"
    input.tool_name in {"observability_query", "runbook_read", "log_search", "shell_exec", "skill_search"}
}

# Adversarial code critic — read-only, same tool surface as code_reviewer's
# tool-gathering step. No write/execute tools.
allow if {
    input.agent_role == "adversarial_code_critic"
    input.tool_name in {"git_diff", "run_linter"}
}

# Skills registry — read-only tools available to all agent roles
# (governance /check strips the server prefix, so short names are used here)
allow if {
    input.agent_role in {"sre", "code_reviewer", "architect", "human_operator"}
    input.tool_name in {
        "list_skills", "get_skill", "get_skill_prompt",
        "list_episodes", "get_episode",
        "list_candidates", "get_candidate",
        "execute_skill",
    }
}

# Skills registry — labeling and proposal available to sre and code_reviewer
allow if {
    input.agent_role in {"sre", "code_reviewer"}
    input.tool_name in {"label_episode", "propose_candidate"}
}

# Skills registry — write/management tools restricted to human_operator
allow if {
    input.agent_role == "human_operator"
    input.tool_name in {
        "create_skill", "revoke_skill",
        "promote_candidate", "reject_candidate",
    }
}

# ---------------------------------------------------------------------------
# Inter-agent invocation topology
# ---------------------------------------------------------------------------

allowed_targets := {
    "supervisor":    ["code-reviewer", "architect", "sre"],
    "architect":     ["code-reviewer"],
    "code_reviewer": [],
    "sre":           [],
}

invoke_allowed[target] if {
    input.action == "invoke"
    target = allowed_targets[input.role][_]
}

# Task claim: a principal may only claim tasks that match their own role
claim_allowed if {
    input.action == "claim"
    input.required_role == input.role
}

# Episode labeling scope — sre and code_reviewer are natural outcome observers;
# human_operator can also label (registry server always uses operator credentials)
label_allowed if {
    input.scope == "episode:label"
    input.agent_role in {"sre", "code_reviewer", "human_operator"}
}

# Candidate proposal scope — same roles propose skill candidates
propose_allowed if {
    input.scope == "candidate:propose"
    input.agent_role in {"sre", "code_reviewer", "human_operator"}
}

# Skill promote/reject scope — human operators only; intentionally NOT in any agent role
promote_allowed if {
    input.scope == "skill:promote"
    input.agent_role == "human_operator"
}

# Manual skill authoring — separate scope from promote for audit granularity; same role
author_allowed if {
    input.scope == "skill:author"
    input.agent_role == "human_operator"
}
