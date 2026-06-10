from typing import TypedDict


class HarnessState(TypedDict):
    task:                    str
    diff:                    str
    task_type:               str | None      # 'design' | 'review' | 'incident'
    formula_id:              str | None
    formula_instance_id:     str | None
    active_agent:            str | None
    agent_output:            dict | None
    final_response:          str | None
    human_approval_token:    str | None
    requires_human_approval: bool
    error:                   dict | None
    thread_id:               str
    memory_context:          list | None
    tokens_used:             int             # running LLM token total for this thread
    token_budget:            int | None      # None = unlimited; graph halts when exceeded
