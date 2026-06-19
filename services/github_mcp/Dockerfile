FROM python:3.12-slim
WORKDIR /app

RUN pip install "mcp[cli]" uvicorn httpx --quiet

COPY services/github_mcp/server.py /app/server.py

CMD ["python", "server.py"]
