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
