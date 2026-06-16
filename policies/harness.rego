package harness

default allow = false

allow if {
    input.agent_role == "architect"
    input.tool_name in {"codebase_search", "adr_read", "adr_write", "diagram_gen"}
}

allow if {
    input.agent_role == "code_reviewer"
    input.tool_name in {"git_diff", "run_linter", "coverage_report", "repo_conventions_read", "review_diff"}
}

allow if {
    input.agent_role == "sre"
    input.tool_name in {"observability_query", "runbook_read", "log_search", "shell_exec"}
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

# Episode labeling scope — sre and code_reviewer are natural outcome observers
label_allowed if {
    input.scope == "episode:label"
    input.agent_role in {"sre", "code_reviewer"}
}

# Candidate proposal scope — same roles propose skill candidates
propose_allowed if {
    input.scope == "candidate:propose"
    input.agent_role in {"sre", "code_reviewer"}
}

# Skill promote/reject scope — human operators only; intentionally NOT in any agent role
promote_allowed if {
    input.scope == "skill:promote"
    input.agent_role == "human_operator"
}
