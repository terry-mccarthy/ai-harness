FROM python:3.12-slim
WORKDIR /app

RUN pip install "mcp[cli]" uvicorn httpx --quiet

COPY packages/harness-gateway /app/packages/harness-gateway
RUN pip install -e /app/packages/harness-gateway --quiet

COPY services/skills_registry/server.py /app/server.py

CMD ["python", "server.py"]
