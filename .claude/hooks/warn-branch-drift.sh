#!/usr/bin/env bash
# UserPromptSubmit hook: warns once if the branch has changed since this
# session started. OTEL project.branch attribution is frozen at Claude Code
# startup (see docs/dev/monitoring.md), so usage after a mid-session branch
# switch keeps being attributed to the branch the session began on.

STATE_DIR="$CLAUDE_PROJECT_DIR/.claude/.state"
STARTUP_BRANCH_FILE="$STATE_DIR/session-branch"
WARNED_FILE="$STATE_DIR/branch-drift-warned"

[ -f "$STARTUP_BRANCH_FILE" ] || exit 0
[ -f "$WARNED_FILE" ] && exit 0

STARTUP_BRANCH=$(cat "$STARTUP_BRANCH_FILE")
CURRENT_BRANCH=$(git -C "$CLAUDE_PROJECT_DIR" branch --show-current 2>/dev/null)

if [ -n "$STARTUP_BRANCH" ] && [ -n "$CURRENT_BRANCH" ] && [ "$STARTUP_BRANCH" != "$CURRENT_BRANCH" ]; then
  touch "$WARNED_FILE"
  MSG="Heads up: the git branch changed from '$STARTUP_BRANCH' to '$CURRENT_BRANCH' during this session. OTEL project.branch telemetry is frozen at session startup and will keep being attributed to '$STARTUP_BRANCH' until this Claude Code session is restarted. Mention this to the user."
  jq -n --arg msg "$MSG" '{hookSpecificOutput: {hookEventName: "UserPromptSubmit", additionalContext: $msg}}'
fi

exit 0
