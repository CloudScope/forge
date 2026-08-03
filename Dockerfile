FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FORGE_VAR_ROOT=/var/forge \
    PORT=8787

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY forge ./forge
COPY config ./config
COPY ops ./ops
COPY deploy ./deploy

RUN mkdir -p /var/forge/state /var/forge/artifacts /var/forge/uploads \
    /var/forge/workspaces /var/forge/deliverables

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/health" || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "2"]
