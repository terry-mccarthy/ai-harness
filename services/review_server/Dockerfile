FROM python:3.12-slim
WORKDIR /app

# Install pinned external deps
COPY services/review_server/requirements.txt .
RUN pip install --no-deps -r requirements.txt

# Copy local packages and install
COPY packages/harness-gateway /app/packages/harness-gateway
COPY packages/harness-agents /app/packages/harness-agents
RUN pip install -e /app/packages/harness-gateway -e /app/packages/harness-agents

COPY prompts /app/prompts
COPY services/review_server/metrics.py /app/metrics.py
COPY services/review_server/server.py /app/server.py
COPY services/review_server/architecture_review.py /app/architecture_review.py
COPY services/review_server/architecture_gate/ /app/architecture_gate/

CMD ["python", "server.py"]
