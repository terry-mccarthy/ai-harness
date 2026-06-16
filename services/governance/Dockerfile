FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY server.py .
COPY core/ ./core/
COPY routers/ ./routers/
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]
