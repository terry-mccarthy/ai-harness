from typing import TypedDict


class AgentState(TypedDict, total=False):
    task: str
    task_type: str
    diff: str
    thread_id: str
    agent_output: dict | None
    requires_human_approval: bool
    error: dict | None
    human_approval_token: str | None
    memory_context: list | None
    token_usage: dict           # {"prompt_tokens": int, "completion_tokens": int}
    token_budget: int | None    # None = unlimited; agent aborts retries when exceeded
    formula: object | None      # pre-loaded Formula injected by run_agent_node
    force_refresh: bool         # skip cache lookup and write for this request
    cache_hit: bool             # True when result was served from the semantic cache
    first_pass_output: dict | None  # first-pass agent output an adversarial critic attacks


# Matches the Phase-4 synthesis output produced by prompts/architect.md.
# The architect now emits an architecture-review report, not an ADR. Extra keys
# (e.g. the `_phases` trace appended by ArchitectAgent.run) are tolerated.
_SEVERITY = {"type": "string", "enum": ["CRITICAL", "HIGH", "MEDIUM", "LOW"]}

ARCHITECT_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["title", "status", "summary", "findings", "recommendations"],
    "properties": {
        "title":   {"type": "string"},
        "status":  {"type": "string"},
        "summary": {"type": "string"},
        "current_state_assessment": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["severity", "message"],
                "properties": {
                    "severity": _SEVERITY,
                    # category is a free string: prompts/architect.md suggests a
                    # vocabulary (modularity/coupling/abstraction/layering/
                    # scalability/security) but an off-list tag must not void an
                    # otherwise-valid review at runtime.
                    "category":     {"type": "string"},
                    "title":        {"type": "string"},
                    "message":      {"type": "string"},
                    "location":     {"type": "string"},
                    "phase_origin": {"type": "string"},
                },
            },
        },
        "technical_debt_hotspots": {"type": "array"},
        "nfr_risks": {"type": "array"},
        "recommendations": {"type": "array"},
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
    # additionalProperties intentionally open: run() appends `_phases`.
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

_CRITIC_OUTCOME = {
    "type": "string",
    "enum": ["confirmed", "refuted", "escalated", "downgraded", "unresolved"],
}

# Adversarial code critic: attacks the first-pass CodeReviewerAgent findings.
# A confirmed/escalated CRITICAL finding requires a concrete exploit_scenario —
# the "forced artifact" that stands in for a bare severity label. Every other
# outcome/severity combination leaves exploit_scenario optional.
ADVERSARIAL_CODE_CRITIC_SCHEMA = {
    "type": "object",
    "required": ["findings", "summary"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["outcome", "severity", "file", "line", "message"],
                "properties": {
                    "outcome":  _CRITIC_OUTCOME,
                    "severity": {"type": "string", "enum": ["INFO", "WARNING", "CRITICAL"]},
                    "file":     {"type": "string"},
                    "line":     {"type": "integer"},
                    "message":  {"type": "string"},
                    "exploit_scenario": {"type": "string"},
                },
                "if": {
                    "properties": {
                        "outcome":  {"enum": ["confirmed", "escalated"]},
                        "severity": {"const": "CRITICAL"},
                    },
                    "required": ["outcome", "severity"],
                },
                "then": {
                    "required": ["exploit_scenario"],
                    "properties": {
                        "exploit_scenario": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "summary": {"type": "string"},
    },
    "additionalProperties": False,
}

# Adversarial architecture critic: attacks the first-pass ArchitectAgent synthesis
# findings. A confirmed/escalated HIGH+ finding requires a concrete
# regression_scenario — the "forced artifact" that stands in for a bare severity
# label. Every other outcome/severity combination leaves regression_scenario optional.
ADVERSARIAL_ARCHITECTURE_CRITIC_SCHEMA = {
    "type": "object",
    "required": ["findings", "summary"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["outcome", "severity", "location", "message"],
                "properties": {
                    "outcome":  _CRITIC_OUTCOME,
                    "severity": _SEVERITY,
                    "location": {"type": "string"},
                    "message":  {"type": "string"},
                    "regression_scenario": {"type": "string"},
                },
                "if": {
                    "properties": {
                        "outcome":  {"enum": ["confirmed", "escalated"]},
                        "severity": {"enum": ["CRITICAL", "HIGH"]},
                    },
                    "required": ["outcome", "severity"],
                },
                "then": {
                    "required": ["regression_scenario"],
                    "properties": {
                        "regression_scenario": {"type": "string", "minLength": 1},
                    },
                },
            },
        },
        "summary": {"type": "string"},
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
