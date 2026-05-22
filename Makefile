SHELL := /bin/bash

.PHONY: stack-up stack-down venv test test-unit test-integration test-e2e review

stack-up:
	docker compose up -d --wait

stack-down:
	docker compose down -v

venv:
	python3 -m venv .venv
	.venv/bin/pip install -e packages/harness-gateway -e packages/harness-agents -e packages/harness-tests

test-unit:
	set -a && source .env && set +a && \
	.venv/bin/pytest packages/harness-tests/ -v -m "not integration and not e2e and not live" || \
	{ ec=$$?; [ $$ec -eq 5 ] && echo "(no unit tests collected)" && exit 0 || exit $$ec; }

test-integration:
	set -a && source .env && set +a && \
	.venv/bin/pytest packages/harness-tests/ -v -m integration

test-e2e:
	set -a && source .env && set +a && \
	.venv/bin/pytest packages/harness-tests/ -v -m e2e || \
	{ ec=$$?; [ $$ec -eq 5 ] && echo "(no e2e tests collected)" && exit 0 || exit $$ec; }

test: test-integration

review:
	@set -a && source .env && set +a && \
	.venv/bin/python -c "\
import asyncio, os, json; \
from harness_agents.llm import OllamaProvider; \
from harness_gateway.client import GatewayClient; \
from harness_agents.reviewer import CodeReviewerAgent; \
from harness_agents.types import AgentState; \
diff = open('sample.diff').read() if os.path.exists('sample.diff') else 'diff --git a/x.py b/x.py\n'; \
agent = CodeReviewerAgent(gateway=GatewayClient(os.environ['MCPJUNGLE_URL'], 'code-reviewer', os.environ['CODE_REVIEWER_SECRET']), llm_provider=OllamaProvider(host=os.environ.get('OLLAMA_HOST', 'http://localhost:11434'), model=os.environ.get('OLLAMA_MODEL', 'qwen2.5-coder'))); \
result = asyncio.run(agent.run(AgentState(task='Review this', diff=diff, thread_id='manual-run', agent_output=None, requires_human_approval=False, error=None))); \
print(json.dumps(result['agent_output'], indent=2))"
