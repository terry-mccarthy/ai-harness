from typing import TypedDict


class AgentState(TypedDict):
    task: str
    diff: str
    thread_id: str
    agent_output: dict | None
    requires_human_approval: bool
    error: dict | None
    human_approval_token: str | None
    memory_context: list | None


ARCHITECT_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["title", "status", "context", "decision", "consequences", "alternatives_considered"],
    "properties": {
        "title":      {"type": "string"},
        "status":     {"type": "string", "enum": ["proposed", "accepted", "deprecated", "superseded"]},
        "context":    {"type": "string"},
        "decision":   {"type": "string"},
        "consequences": {"type": "string"},
        "alternatives_considered": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["option", "reason_rejected"],
                "properties": {
                    "option":          {"type": "string"},
                    "reason_rejected": {"type": "string"},
                },
            },
        },
    },
    "additionalProperties": False,
}

SRE_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["timeline", "likely_cause", "severity", "recommended_steps", "runbook_ref", "requires_human_approval"],
    "properties": {
        "timeline":    {"type": "string"},
        "likely_cause": {"type": "string"},
        "severity":    {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
        "recommended_steps": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["action", "rationale", "requires_approval"],
                "properties": {
                    "action":            {"type": "string"},
                    "rationale":         {"type": "string"},
                    "requires_approval": {"type": "boolean"},
                },
            },
        },
        "runbook_ref":           {"type": ["string", "null"]},
        "requires_human_approval": {"type": "boolean"},
    },
    "additionalProperties": False,
}

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
