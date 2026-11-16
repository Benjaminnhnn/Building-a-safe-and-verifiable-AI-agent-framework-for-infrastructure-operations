#!/usr/bin/env bash
# Local Docker lab for the AIHS LDA/GBDT data campaign. Mirrors the Moodle staging stack closely enough to
# reuse its fault injectors, probes, reset semantics and safe-executor catalog; see "Deviations" below.
#
#   lab/moodle-local/lab.sh build            build the Moodle image (from moodle/), tools image, lb/prometheus/blackbox
#   lab/moodle-local/lab.sh up               start db, 2 Moodle nodes + cron, LB, probes, synthetic, prometheus
#                                            (first run also installs the Moodle database: several minutes)
#   lab/moodle-local/lab.sh status           container + alert overview
#   lab/moodle-local/lab.sh down [-v]        stop the lab (-v also deletes the database and moodledata volumes)
#   lab/moodle-local/lab.sh run <runner args>  run lab_runner.py in a throwaway container, e.g.
#                                            lab.sh run pilot --episodes 24
#                                            lab.sh run episode --scenario DB-01 --action remove_scoped_db_reject
#                                            lab.sh run verify-baseline
#   lab/moodle-local/lab.sh batch <args>     launch the campaign DETACHED (restart=on-failure) from a code snapshot
#   lab/moodle-local/lab.sh fleet <campaign> <episodes> <workers> [seed]   campaign across N side-by-side labs, detached
#   lab/moodle-local/lab.sh progress         per-worker counts, ETA    lab.sh stop-batch   stop all campaign containers
#
# Evidence lands in evaluation/aihs/.artifacts/lab-campaign/ (Git-ignored). Runs on Windows (Git Bash + Docker Desktop)
# or Linux. Only Docker is required; nothing here touches AWS or Terraform.
#
# Deviations from staging (also listed in agent_src/README.md): PostgreSQL has TLS off; ALB = haproxy; EFS = a local
# volume (no NFS caching delay); node CPU is computed from `docker stats` instead of node_exporter; the staging
# `moodle_fault_*` marker metrics are not exported; Prometheus scrapes every 5s; MoodleAdhocTaskQueueStuck uses
# `for: 30s` instead of 5m; cron interval is 20s; both "nodes" share one Docker host so DB-01's DOCKER-USER rule is
# scoped to the app-net address of the database (the monitor reaches the DB over mon-net and stays unaffected).
set -euo pipefail

LAB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd -- "$LAB_DIR/../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then
  REPO_W="$(cygpath -m "$REPO")"; LAB_W="$(cygpath -m "$LAB_DIR")"
else
  REPO_W="$REPO"; LAB_W="$LAB_DIR"
fi
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'
OUT_HOST="$REPO_W/evaluation/aihs/.artifacts/lab-campaign"
# Several labs can run side by side: LAB_PROJECT=aihs-lab2 LAB_PREFIX=lab2 LAB_PROM_PORT=19091 lab.sh up
export LAB_PROJECT="${LAB_PROJECT:-aihs-lab}" LAB_PREFIX="${LAB_PREFIX:-lab}" LAB_PROM_PORT="${LAB_PROM_PORT:-19090}"
COMPOSE=(docker compose -p "$LAB_PROJECT" -f "$LAB_W/docker-compose.yml")
TOOLS=aihs-lab/tools:lab
BATCH="${LAB_PROJECT}-campaign"

runner_args=(--network "${LAB_PROJECT}_mon-net" -e LAB_PROJECT -e LAB_PREFIX
  -v /var/run/docker.sock:/var/run/docker.sock
  -v "$OUT_HOST:/out"
  -e LAB_OUT_DIR=/out -e PYTHONUNBUFFERED=1)

build() {
  docker image inspect aihs-lab/moodle:lab >/dev/null 2>&1 || docker build -t aihs-lab/moodle:lab "$REPO_W/moodle"
  # The repo-root .dockerignore (AI image) hides lab/ and automation/, so stage a minimal named context.
  stage="$REPO/evaluation/aihs/.artifacts/lab-campaign/_build/repo"
  rm -rf "$stage" && mkdir -p "$stage/evaluation/aihs" "$stage/automation"
  cp -r "$REPO/lab" "$stage/lab"
  cp "$REPO/evaluation/aihs/action_ladder.py" "$stage/evaluation/aihs/"
  cp "$REPO/automation/moodle-synthetic-transaction.py" "$stage/automation/"
  docker build -q -f "$LAB_W/images/tools/Dockerfile" --build-context "repo=$OUT_HOST/_build/repo" -t "$TOOLS" "$LAB_W/images/tools"
  docker build -q -t aihs-lab/netfault:lab "$LAB_W/images/netfault"
  "${COMPOSE[@]}" build lb blackbox prometheus
}

install_if_needed() {
  "${COMPOSE[@]}" up -d moodle-db
  until docker exec "$LAB_PREFIX-moodle-db" pg_isready -U moodle_app -d moodle >/dev/null 2>&1; do sleep 2; done
  have="$(docker exec "$LAB_PREFIX-moodle-db" psql -U moodle_app -d moodle -tAc "select to_regclass('public.mdl_config')" | tr -d '[:space:]')"
  if [[ -z "$have" ]]; then
    echo "Installing the Moodle database (first run only)..."
    "${COMPOSE[@]}" --profile installer run --rm moodle-install
  fi
}

case "${1:-}" in
  build) build ;;
  up)
    mkdir -p "$REPO/evaluation/aihs/.artifacts/lab-campaign"
    docker image inspect "$TOOLS" >/dev/null 2>&1 || build
    install_if_needed
    "${COMPOSE[@]}" --profile cron up -d --wait
    ;;
  down) shift; "${COMPOSE[@]}" --profile cron --profile installer down "$@" ;;
  status)
    "${COMPOSE[@]}" --profile cron ps
    docker run --rm "${runner_args[@]}" -v "$REPO_W:/work:ro" -e PYTHONPATH=/work/lab/moodle-local:/work/evaluation/aihs "$TOOLS" \
      python /work/lab/moodle-local/lab_runner.py verify-baseline --quiet || true
    ;;
  run)
    shift
    docker run --rm "${runner_args[@]}" -v "$REPO_W:/work:ro" \
      -e PYTHONPATH=/work/lab/moodle-local:/work/evaluation/aihs -e LAB_COMPOSE_FILE=/work/lab/moodle-local/docker-compose.yml \
      "$TOOLS" python /work/lab/moodle-local/lab_runner.py "$@"
    ;;
  batch)
    shift
    snap="evaluation/aihs/.artifacts/lab-campaign/_code/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$REPO/$snap"
    cp -r "$REPO/lab" "$REPO/$snap/lab"
    mkdir -p "$REPO/$snap/evaluation/aihs" "$REPO/$snap/automation"
    cp "$REPO/evaluation/aihs/action_ladder.py" "$REPO/$snap/evaluation/aihs/"
    cp "$REPO/automation/moodle-synthetic-transaction.py" "$REPO/$snap/automation/"
    docker rm -f "$BATCH" >/dev/null 2>&1 || true
    docker run -d --name "$BATCH" --restart on-failure:20 "${runner_args[@]}" -v "$REPO_W/$snap:/work:ro" \
      -e PYTHONPATH=/work/lab/moodle-local:/work/evaluation/aihs -e LAB_COMPOSE_FILE=/work/lab/moodle-local/docker-compose.yml \
      "$TOOLS" python /work/lab/moodle-local/lab_runner.py batch "$@"
    echo "campaign container: $BATCH (code snapshot: $snap)"
    ;;
  fleet)
    # lab.sh fleet <campaign> <episodes> <workers> [seed]: run one campaign across N side-by-side labs, detached.
    shift; campaign="$1"; episodes="$2"; workers="$3"; seed="${4:-20261115}"
    for ((i = 0; i < workers; i++)); do
      if ((i == 0)); then proj=aihs-lab; pre=lab; port=19090; else proj="aihs-lab$((i + 1))"; pre="lab$((i + 1))"; port=$((19090 + i)); fi
      LAB_PROJECT="$proj" LAB_PREFIX="$pre" LAB_PROM_PORT="$port" bash "${BASH_SOURCE[0]}" up
      LAB_PROJECT="$proj" LAB_PREFIX="$pre" LAB_PROM_PORT="$port" bash "${BASH_SOURCE[0]}" batch         --campaign "$campaign" --episodes "$episodes" --workers "$workers" --worker-index "$i" --seed "$seed"
    done
    ;;
  progress)
    for c in $(docker ps -a --filter "name=-campaign" --format '{{.Names}}'); do
      echo "== $c: $(docker ps -a --filter "name=^$c$" --format '{{.Status}}')"
      docker logs --tail 3 "$c" 2>&1
    done
    cd "$REPO"
    for f in evaluation/aihs/.artifacts/lab-campaign/progress-*.json; do
      [[ -f "$f" ]] && python -c "import json,sys; p=json.load(open(sys.argv[1])); print(p['worker'], p['campaign'], str(p['done'])+'/'+str(p['total']), 'median_s', p['median_episode_seconds'], 'eta_s', p['eta_seconds'], p['summary']['by_outcome'])" "$f"
    done
    ;;
  stop-batch)
    # Stops every campaign container (or only $LAB_PROJECT's when set explicitly); the running episode finishes its reset.
    for c in $(docker ps -a --filter "name=-campaign" --format '{{.Names}}'); do
      docker update --restart=no "$c" >/dev/null && docker stop -t 300 "$c"
    done ;;
  *) sed -n 2,21p "${BASH_SOURCE[0]}"; exit 64 ;;
esac
