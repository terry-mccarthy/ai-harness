FROM python:3.12-slim
WORKDIR /app

# Install Python deps
RUN pip install "mcp[cli]" uvicorn ollama httpx jsonschema

# Copy local packages and install
COPY packages/harness-gateway /app/packages/harness-gateway
COPY packages/harness-agents /app/packages/harness-agents
RUN pip install -e /app/packages/harness-gateway -e /app/packages/harness-agents

COPY services/review_server/server.py /app/server.py

CMD ["python", "server.py"]
