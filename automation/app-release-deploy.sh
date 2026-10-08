#!/usr/bin/env bash
set -euo pipefail

ENVIRONMENT="${1:-}"
NEW_TAG="${2:-}"
DEPLOY_ROLE="${3:-}"

if [[ -z "$ENVIRONMENT" || -z "$NEW_TAG" ]]; then
  echo "Usage: $0 <staging|production> <image-tag> <monitor|web|core>"
  exit 2
fi

if [[ "$ENVIRONMENT" != "staging" && "$ENVIRONMENT" != "production" ]]; then
  echo "Invalid environment: $ENVIRONMENT"
  exit 2
fi

if [[ "$DEPLOY_ROLE" != "monitor" && "$DEPLOY_ROLE" != "web" && "$DEPLOY_ROLE" != "core" ]]; then
  echo "Invalid deploy role: $DEPLOY_ROLE"
  exit 2
fi

case "$ENVIRONMENT" in
  staging)
    COMPOSE_FILE="release/docker-compose.staging.yml"
    ENV_FILE="release/.env.staging"
    AI_HEALTH_URL="http://127.0.0.1:18000/health"
    API_HEALTH_URL="http://127.0.0.1:18080/api/health"
    WEB_HEALTH_URL="http://127.0.0.1:18081/health"
    WEB_API_HEALTH_URL="http://127.0.0.1:18081/api/health"
    ;;
  production)
    COMPOSE_FILE="release/docker-compose.production.yml"
    ENV_FILE="release/.env.production"
    AI_HEALTH_URL="http://127.0.0.1:8000/health"
    API_HEALTH_URL="http://127.0.0.1:8080/api/health"
    WEB_HEALTH_URL="http://127.0.0.1:3000/health"
    WEB_API_HEALTH_URL="http://127.0.0.1:3000/api/health"
    ;;
esac

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "Compose file not found: $COMPOSE_FILE"
  exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "Environment file not found: $ENV_FILE"
  echo "Creating from template..."

  if [[ ! -f "release/.env.example" ]]; then
    echo "Template file not found: release/.env.example"
    exit 1
  fi

  cp release/.env.example "$ENV_FILE"
  echo "Created $ENV_FILE from template. Please configure it with actual values."
  echo "Critical variables needed:"
  echo "  - GHCR_OWNER (GitHub org/username)"
  echo "  - GEMINI_API_KEY (Google Gemini API key)"
  echo "  - TELEGRAM_TOKEN (optional)"
  echo "  - TELEGRAM_CHAT_ID (optional)"
  echo "  - TELEGRAM_WEBHOOK_SECRET and TELEGRAM_ADMIN_USER_IDS (if using Telegram webhook)"
fi

if [[ -n "${GHCR_USERNAME:-}" && -n "${GHCR_TOKEN:-}" ]]; then
  echo "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USERNAME" --password-stdin
else
  echo "GHCR credentials not set in shell; assuming host already logged in"
fi

mkdir -p release/.state
STATE_FILE="release/.state/${ENVIRONMENT}.tag"
PREVIOUS_TAG=""

if [[ -f "$STATE_FILE" ]]; then
  PREVIOUS_TAG="$(cat "$STATE_FILE")"
fi

EXTERNAL_GHCR_OWNER="${GHCR_OWNER:-}"
export GHCR_OWNER="${GHCR_OWNER:-your-org}"
export IMAGE_TAG="$NEW_TAG"
COMPOSE_PROJECT_NAME="aws-hybrid-${ENVIRONMENT}-${DEPLOY_ROLE}"
STALE_CONTAINER_NAMES=()

case "$DEPLOY_ROLE:$ENVIRONMENT" in
  monitor:staging)
    CONTAINER_NAMES=(redis-staging redis-cache-staging redis-exporter-staging redis-broker-exporter-staging ai-agent-staging celery-worker-staging log-watcher-staging)
    STALE_CONTAINER_NAMES=(payment-api-staging postgres-staging postgres-exporter-staging frontend-web-staging payment-api-prod postgres-prod postgres-exporter-prod frontend-web-prod)
    SERVICE_NAMES=(redis redis-cache redis-exporter redis-broker-exporter ai-agent celery-worker log-watcher)
    ;;
  monitor:production)
    CONTAINER_NAMES=(redis-prod redis-cache-prod redis-exporter-prod redis-broker-exporter-prod ai-agent-prod celery-worker-prod log-watcher-prod)
    STALE_CONTAINER_NAMES=(payment-api-staging postgres-staging postgres-exporter-staging frontend-web-staging payment-api-prod postgres-prod postgres-exporter-prod frontend-web-prod)
    SERVICE_NAMES=(redis redis-cache redis-exporter redis-broker-exporter ai-agent celery-worker log-watcher)
    ;;
  web:staging)
    CONTAINER_NAMES=(frontend-web-staging)
    STALE_CONTAINER_NAMES=(redis-staging redis-cache-staging redis-exporter-staging redis-broker-exporter-staging ai-agent-staging celery-worker-staging log-watcher-staging payment-api-staging postgres-staging postgres-exporter-staging redis-prod redis-cache-prod redis-exporter-prod redis-broker-exporter-prod ai-agent-prod celery-worker-prod log-watcher-prod payment-api-prod postgres-prod postgres-exporter-prod)
    SERVICE_NAMES=(frontend-web)
    ;;
  web:production)
    CONTAINER_NAMES=(frontend-web-prod)
    STALE_CONTAINER_NAMES=(redis-staging redis-cache-staging redis-exporter-staging redis-broker-exporter-staging ai-agent-staging celery-worker-staging log-watcher-staging payment-api-staging postgres-staging postgres-exporter-staging redis-prod redis-cache-prod redis-exporter-prod redis-broker-exporter-prod ai-agent-prod celery-worker-prod log-watcher-prod payment-api-prod postgres-prod postgres-exporter-prod)
    SERVICE_NAMES=(frontend-web)
    ;;
  core:staging)
    CONTAINER_NAMES=(postgres-staging postgres-exporter-staging payment-api-staging)
    STALE_CONTAINER_NAMES=(redis-staging redis-cache-staging redis-exporter-staging redis-broker-exporter-staging ai-agent-staging celery-worker-staging log-watcher-staging frontend-web-staging redis-prod redis-cache-prod redis-exporter-prod redis-broker-exporter-prod ai-agent-prod celery-worker-prod log-watcher-prod frontend-web-prod)
    SERVICE_NAMES=(postgres postgres-exporter payment-api)
    ;;
  core:production)
    CONTAINER_NAMES=(postgres-prod postgres-exporter-prod payment-api-prod)
    STALE_CONTAINER_NAMES=(redis-staging redis-cache-staging redis-exporter-staging redis-broker-exporter-staging ai-agent-staging celery-worker-staging log-watcher-staging frontend-web-staging redis-prod redis-cache-prod redis-exporter-prod redis-broker-exporter-prod ai-agent-prod celery-worker-prod log-watcher-prod frontend-web-prod)
    SERVICE_NAMES=(postgres postgres-exporter payment-api)
    ;;
esac

load_env_file() {
  # Export key=value pairs from env file for docker-compose fallback.
  local tag_backup="${IMAGE_TAG:-}"

  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a

  # Keep runtime deployment values authoritative over template defaults.
  if [[ -n "$EXTERNAL_GHCR_OWNER" ]]; then
    export GHCR_OWNER="$EXTERNAL_GHCR_OWNER"
  elif [[ -z "${GHCR_OWNER:-}" ]]; then
    export GHCR_OWNER="your-org"
  fi
  if [[ -n "$tag_backup" ]]; then
    export IMAGE_TAG="$tag_backup"
  fi
}

compose_cmd() {
  if docker compose version >/dev/null 2>&1; then
    docker compose -p "$COMPOSE_PROJECT_NAME" --env-file "$ENV_FILE" -f "$COMPOSE_FILE" "$@"
    return
  fi

  if command -v docker-compose >/dev/null 2>&1; then
    load_env_file
    docker-compose -p "$COMPOSE_PROJECT_NAME" -f "$COMPOSE_FILE" "$@"
    return
  fi

  echo "No compose runtime found. Install Docker Compose plugin or docker-compose binary."
  exit 1
}

migrate_compose_project_containers() {
  local container
  local current_project

  for container in "${CONTAINER_NAMES[@]}"; do
    if ! docker container inspect "$container" >/dev/null 2>&1; then
      continue
    fi

    current_project="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$container" 2>/dev/null || true)"
    if [[ "$current_project" == "$COMPOSE_PROJECT_NAME" ]]; then
      continue
    fi

    echo "Migrating $container from compose project '${current_project:-none}' to '$COMPOSE_PROJECT_NAME'"
    docker rm -f "$container"
  done
}

remove_out_of_role_containers() {
  local container

  for container in "${STALE_CONTAINER_NAMES[@]}"; do
    if docker container inspect "$container" >/dev/null 2>&1; then
      echo "Removing out-of-role container from this host: $container"
      docker rm -f "$container"
    fi
  done
}

prepare_role_runtime_files() {
  if [[ "$DEPLOY_ROLE" == "monitor" ]]; then
    if [[ -d /tmp/aiops-test-syslog.log ]]; then
      sudo rm -rf /tmp/aiops-test-syslog.log
    fi

    if [[ ! -e /tmp/aiops-test-syslog.log ]]; then
      install -m 0644 /dev/null /tmp/aiops-test-syslog.log 2>/dev/null \
        || sudo install -o ec2-user -g ec2-user -m 0644 /dev/null /tmp/aiops-test-syslog.log
    fi
  fi
}

validate_release_env() {
  if [[ "$DEPLOY_ROLE" != "web" ]]; then
    return
  fi

  load_env_file
  case "${PAYMENT_API_UPSTREAM:-}" in
    http://*|https://*)
      ;;
    *)
      echo "PAYMENT_API_UPSTREAM must include http:// or https://"
      echo "Example for staging: PAYMENT_API_UPSTREAM=http://10.10.1.171:18080"
      exit 1
      ;;
  esac
}

available_disk_mb() {
  df -Pm / | awk 'NR == 2 {print $4}'
}

ensure_docker_disk_space() {
  local min_free_mb="${MIN_DOCKER_FREE_MB:-12288}"
  local free_mb

  free_mb="$(available_disk_mb)"
  if [[ "$free_mb" -ge "$min_free_mb" ]]; then
    echo "Disk preflight OK: ${free_mb}MB free on /"
    return
  fi

  echo "Disk preflight warning: only ${free_mb}MB free on /; need at least ${min_free_mb}MB"
  docker system df || true
  echo "Pruning unused Docker images and build cache before pulling new release..."
  docker image prune -af
  docker builder prune -af || true

  free_mb="$(available_disk_mb)"
  if [[ "$free_mb" -lt "$min_free_mb" ]]; then
    echo "Still only ${free_mb}MB free after Docker image prune."
    docker system df || true
    echo "Increase the EC2 root volume or remove unused Docker data before deploying."
    exit 1
  fi

  echo "Disk preflight recovered: ${free_mb}MB free on /"
}

health_check() {
  local retries=18
  local wait_seconds=10

  for attempt in $(seq 1 "$retries"); do
    ai_ok=1
    api_ok=1
    web_ok=1

    if [[ "$DEPLOY_ROLE" == "monitor" ]]; then
      ai_ok=0
      if curl -fsS "$AI_HEALTH_URL" >/dev/null; then
        ai_ok=1
      fi
    fi

    if [[ "$DEPLOY_ROLE" == "core" ]]; then
      api_ok=0
      if curl -fsS "$API_HEALTH_URL" >/dev/null; then
        api_ok=1
      fi
    fi

    if [[ "$DEPLOY_ROLE" == "web" ]]; then
      web_ok=0
      if curl -fsS "$WEB_HEALTH_URL" >/dev/null && curl -fsS "$WEB_API_HEALTH_URL" >/dev/null; then
        web_ok=1
      fi
    fi

    if [[ "$ai_ok" -eq 1 && "$api_ok" -eq 1 && "$web_ok" -eq 1 ]]; then
      return 0
    fi

    echo "Health check attempt $attempt/$retries failed; retrying in ${wait_seconds}s"
    sleep "$wait_seconds"
  done

  return 1
}

echo "Deploying $ENVIRONMENT role $DEPLOY_ROLE with tag $NEW_TAG"
validate_release_env
prepare_role_runtime_files
ensure_docker_disk_space
compose_cmd pull "${SERVICE_NAMES[@]}"
remove_out_of_role_containers
migrate_compose_project_containers
compose_cmd up -d --remove-orphans "${SERVICE_NAMES[@]}"

if health_check; then
  echo "$NEW_TAG" > "$STATE_FILE"
  docker image prune -f >/dev/null || true
  echo "Deploy successful. Current tag: $NEW_TAG"
  exit 0
fi

echo "Health check failed for tag: $NEW_TAG"

if [[ -n "$PREVIOUS_TAG" && "$PREVIOUS_TAG" != "$NEW_TAG" ]]; then
  echo "Rolling back to previous tag: $PREVIOUS_TAG"
  export IMAGE_TAG="$PREVIOUS_TAG"
  compose_cmd pull "${SERVICE_NAMES[@]}"
  compose_cmd up -d --remove-orphans "${SERVICE_NAMES[@]}"

  if health_check; then
    echo "Rollback successful to tag: $PREVIOUS_TAG"
  else
    echo "Rollback failed. Manual intervention required."
  fi
else
  echo "No previous tag available for rollback."
fi

exit 1
