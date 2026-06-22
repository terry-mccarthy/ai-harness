SHELL := /bin/bash

.PHONY: stack-up stack-down venv requirements test test-unit test-integration test-e2e test-load review consolidate alembic-upgrade monitoring-up seed-runbooks seed-logs demo-sre

stack-up:
	docker compose up -d --wait --remove-orphans

stack-down:
	docker compose down -v --remove-orphans

venv:
	uv sync --all-packages

requirements:
	uv export --frozen --no-color --package governance    --no-emit-project > services/governance/requirements.txt
	uv export --frozen --no-color --package review-server --no-emit-project > services/review_server/requirements.txt
	uv export --frozen --no-color --package stub-servers  --no-emit-project > stub_servers/requirements.txt

test-fast:
	.venv/bin/pytest packages/harness-tests/ -v \
	  -m "integration and not live" \
	  --ignore=packages/harness-tests/test_review_mcp.py \
	  --ignore=packages/harness-tests/test_thin_slice.py

test-unit:
	.venv/bin/pytest packages/harness-tests/ -v -m "not integration and not e2e and not live" || \
	{ ec=$$?; [ $$ec -eq 5 ] && echo "(no unit tests collected)" && exit 0 || exit $$ec; }

test-integration:
	.venv/bin/pytest packages/harness-tests/ -v -m integration

test-e2e:
	.venv/bin/pytest packages/harness-tests/ -v -m e2e || \
	{ ec=$$?; [ $$ec -eq 5 ] && echo "(no e2e tests collected)" && exit 0 || exit $$ec; }

test-load:
	.venv/bin/pytest packages/harness-tests/test_phase5_load.py -v -s -m load

monitoring-up:
	docker compose --profile monitoring up -d

test: test-integration

consolidate:
	set -a && source .env && set +a && \
	.venv/bin/python -c "\
import asyncio, os; \
from harness_memory.memory_store import PostgresMemoryStore; \
from harness_memory.formula_store import DoltFormulaStore; \
from harness_memory.consolidation import ConsolidationWorker; \
store = PostgresMemoryStore(os.environ['PG_DSN'], os.environ.get('REDIS_URL','redis://localhost:6379'), os.environ.get('EMBED_MODEL','nomic-embed-text'), os.environ.get('OLLAMA_HOST','http://localhost:11434')); \
fstore = DoltFormulaStore(host=os.environ.get('DOLT_HOST','localhost'), port=int(os.environ.get('DOLT_PORT','3306')), user='root', password='root', database='harness'); \
worker = ConsolidationWorker(store=store, formula_store=fstore); \
result = asyncio.run(worker.run_pass('sre')); \
print(result)"

seed-runbooks:
	set -a && source .env && set +a && .venv/bin/python scripts/seed_runbooks.py

seed-logs:
	set -a && source .env && set +a && .venv/bin/python scripts/seed_logs.py

demo-sre:
	set -a && source .env && set +a && .venv/bin/python scripts/demo_sre.py

alembic-upgrade:
	set -a && source .env && set +a && \
	cd packages/harness-memory && ../../.venv/bin/alembic upgrade head

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
