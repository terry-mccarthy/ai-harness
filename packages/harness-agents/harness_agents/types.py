from typing import TypedDict


class AgentState(TypedDict):
    task: str
    diff: str
    thread_id: str
    agent_output: dict | None
    requires_human_approval: bool
    error: dict | None


REVIEWER_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["verdict", "findings", "summary"],
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["pass", "fail"]
        },
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "file", "line", "message", "suggestion"],
                "properties": {
                    "severity":   {"type": "string", "enum": ["INFO", "WARNING", "CRITICAL"]},
                    "file":       {"type": "string"},
                    "line":       {"type": "integer"},
                    "message":    {"type": "string"},
                    "suggestion": {"type": "string"},
                }
            }
        },
        "summary": {"type": "string"},
    },
    "additionalProperties": False,
}
