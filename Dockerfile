# Corey Mathie, 2026
# Gateway image for docker compose, Kubernetes (deploy/helm/local-llm-gateway) and air-gapped installs.
# Includes demo/ (the console, served at /console) and scripts/ (mock backend, ML-BOM).
# Not built in the repository's CI; build and push it to your own registry:
#   docker build -t registry.example.internal/local-llm-gateway:0.7.0 .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# Offline builds: put the wheels from scripts/airgap_bundle.sh in ./wheels and pass --build-arg OFFLINE=1.
ARG OFFLINE=0
# "wheel[s]" matches ./wheels if present and nothing otherwise (requirements.txt keeps the COPY valid).
COPY requirements.txt wheel[s] /tmp/build/
RUN if [ "$OFFLINE" = "1" ]; then pip install --no-index --find-links /tmp/build -r /tmp/build/requirements.txt; \
    else pip install -r /tmp/build/requirements.txt; fi && rm -rf /tmp/build

COPY gateway /app/gateway
COPY admin-ui /app/admin-ui
COPY demo /app/demo
COPY scripts /app/scripts

# Unprivileged user; all writable state lives under /data (a volume), so the root filesystem can be read-only.
RUN useradd --system --uid 10001 --home-dir /data --shell /usr/sbin/nologin gateway \
    && mkdir -p /data/logs && chown -R 10001:10001 /data
USER 10001
ENV GATEWAY_HOST=0.0.0.0 GATEWAY_PORT=8080 GATEWAY_DB_PATH=/data/gateway.db GATEWAY_LOG_DIR=/data/logs
EXPOSE 8080
CMD ["uvicorn", "gateway.main:app", "--host", "0.0.0.0", "--port", "8080"]
