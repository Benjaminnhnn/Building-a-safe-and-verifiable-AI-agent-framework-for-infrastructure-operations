# Probe / synthetic / runner image. Build with:
#   docker build -f lab/moodle-local/images/tools/Dockerfile --build-context repo=<repo root> lab/moodle-local/images/tools
FROM docker:27-cli AS dockercli
FROM python:3.11-slim
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=dockercli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
RUN pip install --no-cache-dir cryptography==48.0.0 jsonschema \
    && useradd --uid 1000 --create-home lab
COPY --from=repo lab/moodle-local /app/lab/moodle-local
COPY --from=repo evaluation/aihs/action_ladder.py /app/evaluation/aihs/action_ladder.py
COPY --from=repo automation/moodle-synthetic-transaction.py /app/automation/moodle-synthetic-transaction.py
ENV PYTHONPATH=/app/lab/moodle-local:/app/evaluation/aihs PYTHONUNBUFFERED=1
WORKDIR /app
