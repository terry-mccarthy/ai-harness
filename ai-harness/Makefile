stack-up:
	docker compose up -d --wait

stack-down:
	docker compose down -v

test:
	pytest packages/harness-tests/test_thin_slice.py -v -m integration

review:
	@echo "Running a sample review..."
	python -c "
import asyncio, os
from ollama import AsyncClient
from harness_gateway.client import GatewayClient
from harness_agents.reviewer import CodeReviewerAgent
from harness_agents.types import AgentState

diff = open('sample.diff').read() if __import__('os').path.exists('sample.diff') else 'diff --git a/x.py'
agent = CodeReviewerAgent(
    gateway=GatewayClient(os.environ['MCPJUNGLE_URL'], 'code-reviewer', os.environ['CODE_REVIEWER_SECRET']),
    llm_client=AsyncClient(host=os.environ.get('OLLAMA_HOST', 'http://localhost:11434')),
    model=os.environ.get('OLLAMA_MODEL', 'qwen2.5-coder'),
)
result = asyncio.run(agent.run(AgentState(task='Review this', diff=diff, thread_id='manual-run', agent_output=None, requires_human_approval=False, error=None)))
import json; print(json.dumps(result['agent_output'], indent=2))
"
