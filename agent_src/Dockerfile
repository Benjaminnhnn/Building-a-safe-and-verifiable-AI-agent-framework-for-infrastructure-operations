FROM python:3.11-slim

WORKDIR /app

# Install curl and ping for health check and diagnostics
RUN apt-get update && apt-get install -y curl iputils-ping && rm -rf /var/lib/apt/lists/*

COPY agent_src/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent_src/ .
# Scenario-bound Moodle alert handling needs the same validated catalog used
# by local tests and replay. Keep these fixtures in the immutable runtime image.
COPY evaluation/ground_truth/ /app/evaluation/ground_truth/

# Default runtime for AI webhook service.
# Restructured main app is now in core/main.py
CMD ["python", "-m", "uvicorn", "core.main:app", "--host", "0.0.0.0", "--port", "8000"]
