package harness

default allow = false

allow if {
    input.agent_role == "code_reviewer"
    input.tool_name in {"git_diff", "run_linter"}
}
