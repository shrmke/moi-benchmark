#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "$script_dir/../../.." && pwd)"
ASTRA_SOURCE_DIR="${ASTRA_SOURCE_DIR:-$workspace_root/external/astra-optimize_0731_05}"
COMPOSE_DIR="$ASTRA_SOURCE_DIR/deployment/all-in-one"
COMPOSE_FILE="$COMPOSE_DIR/docker-compose.deps.yml"
BENCHMARK_API_OVERRIDE="$workspace_root/astra/runners/astra_terminal_bench/docker-compose.benchmark-timeout.yml"
SERVICE_START_TIMEOUT_SECONDS="${SERVICE_START_TIMEOUT_SECONDS:-180}"
HEALTH_INTERVAL_SECONDS="${HEALTH_INTERVAL_SECONDS:-2}"
API_START_TIMEOUT_SECONDS="${API_START_TIMEOUT_SECONDS:-180}"
DEV_MEMORIA_MASTER_KEY="change-me-min-16-chars"
ASTRA_DOCKER_IMAGE="${ASTRA_DOCKER_IMAGE:-astra-optimize-0731-05:local}"
DEV_ASTRA_JWT_SECRET="dev-astra-jwt-secret-min-32-characters"
DEV_ASTRA_BRIDGE_SECRET="dev-astra-bridge-secret-min-32-characters"
DEV_ASTRA_TOKEN_ENCRYPTION_KEY="MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="

for timeout_name in SERVICE_START_TIMEOUT_SECONDS HEALTH_INTERVAL_SECONDS API_START_TIMEOUT_SECONDS; do
  timeout_value="${!timeout_name}"
  if [[ ! "$timeout_value" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: $timeout_name must be a positive integer." >&2
    exit 1
  fi
done

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ERROR: MatrixOne compose file not found: $COMPOSE_FILE" >&2
  exit 1
fi
if [[ ! -f "$BENCHMARK_API_OVERRIDE" ]]; then
  echo "ERROR: Astra benchmark API override not found: $BENCHMARK_API_OVERRIDE" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "ERROR: Docker is not running. Start Docker Desktop and retry." >&2
  exit 1
fi

mkdir -p "$COMPOSE_DIR/data/matrixone/logs"

compose=(docker compose -f "$COMPOSE_FILE")
compose_env=(env UID="$(id -u)" GID="$(id -g)")
if [[ -f "$ASTRA_SOURCE_DIR/.env" ]]; then
  set -a
  # shellcheck source=/dev/null
  source "$ASTRA_SOURCE_DIR/.env"
  set +a
  compose+=(--env-file "$ASTRA_SOURCE_DIR/.env")
  memoria_master_key="${MEMORIA_MASTER_KEY:-}"
  api_host_port="${ASTRA_API_PORT:-17001}"
else
  memoria_host_port="${MEMORIA_HOST_PORT:-18100}"
  api_host_port="${ASTRA_API_HOST_PORT:-17101}"
  compose_env+=(
    MEMORIA_MASTER_KEY="$DEV_MEMORIA_MASTER_KEY"
    MEMORIA_PORT="$memoria_host_port"
  )
  memoria_master_key="$DEV_MEMORIA_MASTER_KEY"
fi

compose_cmd() {
  "${compose_env[@]}" "${compose[@]}" "$@"
}

wait_for_service() {
  local service="$1"
  local started_at=$SECONDS
  local container_id=""
  local state=""
  local health=""

  echo "Waiting for $service health (timeout: ${SERVICE_START_TIMEOUT_SECONDS}s)..."
  while ((SECONDS - started_at < SERVICE_START_TIMEOUT_SECONDS)); do
    container_id="$(compose_cmd ps -q "$service")"
    if [[ -n "$container_id" ]]; then
      state="$(docker inspect --format '{{.State.Status}}' "$container_id" 2>/dev/null || true)"
      health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container_id" 2>/dev/null || true)"
      if [[ "$health" == "healthy" ]]; then
        echo "$service is healthy"
        return 0
      fi
      if [[ "$state" == "exited" || "$state" == "dead" ]]; then
        echo "ERROR: $service stopped before becoming healthy." >&2
        compose_cmd logs --tail=100 "$service" >&2
        return 1
      fi
    fi
    sleep "$HEALTH_INTERVAL_SECONDS"
  done

  echo "ERROR: $service did not become healthy within ${SERVICE_START_TIMEOUT_SECONDS}s." >&2
  compose_cmd logs --tail=100 "$service" >&2
  return 1
}

wait_for_memoria_storage() {
  local started_at=$SECONDS
  local memoria_address=""
  local memoria_port=""

  if [[ -z "$memoria_master_key" ]]; then
    echo "ERROR: MEMORIA_MASTER_KEY is required for the Memoria readiness check." >&2
    return 1
  fi

  memoria_address="$(compose_cmd port memoria 8100)"
  memoria_port="${memoria_address##*:}"
  echo "Waiting for Memoria storage readiness (timeout: ${SERVICE_START_TIMEOUT_SECONDS}s)..."
  while ((SECONDS - started_at < SERVICE_START_TIMEOUT_SECONDS)); do
    if curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 \
      -H "Authorization: Bearer $memoria_master_key" \
      "http://127.0.0.1:${memoria_port}/v1/health/analyze" >/dev/null 2>&1; then
      echo "Memoria storage is ready"
      return 0
    fi
    sleep "$HEALTH_INTERVAL_SECONDS"
  done

  echo "ERROR: Memoria storage did not become ready within ${SERVICE_START_TIMEOUT_SECONDS}s." >&2
  compose_cmd logs --tail=100 memoria >&2
  return 1
}

cd "$COMPOSE_DIR"
echo "Starting MatrixOne and Memoria from: $ASTRA_SOURCE_DIR"
compose_cmd up -d matrixone memoria

wait_for_service matrixone
wait_for_service memoria
wait_for_memoria_storage

sql_address="$(compose_cmd port matrixone 6001)"
sql_port="${sql_address##*:}"
memoria_address="$(compose_cmd port memoria 8100)"
memoria_port="${memoria_address##*:}"
echo "MatrixOne is ready: 127.0.0.1:${sql_port}"
echo "Memoria is ready: 127.0.0.1:${memoria_port}"

cd "$ASTRA_SOURCE_DIR"
echo "Starting Astra API in Docker..."
if [[ "${ASTRA_DOCKER_REBUILD:-0}" == "1" ]] || \
  ! docker image inspect "$ASTRA_DOCKER_IMAGE" >/dev/null 2>&1; then
  echo "Building Astra API Linux image from: $ASTRA_SOURCE_DIR"
  docker build -t "$ASTRA_DOCKER_IMAGE" "$ASTRA_SOURCE_DIR"
fi

api_compose=(
  docker compose
  -f "$COMPOSE_DIR/docker-compose.yml"
  -f "$BENCHMARK_API_OVERRIDE"
)
api_compose_env=(
  env
  UID="$(id -u)"
  GID="$(id -g)"
  ASTRA_IMAGE="$ASTRA_DOCKER_IMAGE"
  ASTRA_API_PORT="$api_host_port"
  ASTRA_ALLOW_INSECURE_DEFAULTS="${ASTRA_ALLOW_INSECURE_DEFAULTS:-1}"
  ASTRA_AUTO_CREATE_DATABASE="${ASTRA_AUTO_CREATE_DATABASE:-1}"
  ASTRA_JWT_SECRET="${ASTRA_JWT_SECRET:-$DEV_ASTRA_JWT_SECRET}"
  ASTRA_TOKEN_ENCRYPTION_KEY="${ASTRA_TOKEN_ENCRYPTION_KEY:-$DEV_ASTRA_TOKEN_ENCRYPTION_KEY}"
  ASTRA_BRIDGE_SECRET="${ASTRA_BRIDGE_SECRET:-$DEV_ASTRA_BRIDGE_SECRET}"
  MEMORIA_MASTER_KEY="$memoria_master_key"
  MEMORIA_EMBEDDING_API_KEY="${MEMORIA_EMBEDDING_API_KEY:-unused}"
  MEMORIA_EMBEDDING_BASE_URL="${MEMORIA_EMBEDDING_BASE_URL:-http://unused.invalid}"
)
"${api_compose_env[@]}" "${api_compose[@]}" up -d --no-deps api

api_container_id="$("${api_compose_env[@]}" "${api_compose[@]}" ps -q api)"
api_started_at=$SECONDS
echo "Waiting for Astra API health (timeout: ${API_START_TIMEOUT_SECONDS}s)..."
while ((SECONDS - api_started_at < API_START_TIMEOUT_SECONDS)); do
  api_state="$(docker inspect --format '{{.State.Status}}' "$api_container_id" 2>/dev/null || true)"
  api_container_health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$api_container_id" 2>/dev/null || true)"
  if [[ "$api_container_health" == "healthy" ]]; then
    break
  fi
  if [[ "$api_state" == "exited" || "$api_state" == "dead" ]]; then
    echo "ERROR: Astra API stopped before becoming healthy." >&2
    "${api_compose_env[@]}" "${api_compose[@]}" logs --tail=100 api >&2
    exit 1
  fi
  sleep "$HEALTH_INTERVAL_SECONDS"
done

if [[ "$api_container_health" != "healthy" ]]; then
  echo "ERROR: Astra API did not become healthy within ${API_START_TIMEOUT_SECONDS}s." >&2
  "${api_compose_env[@]}" "${api_compose[@]}" logs --tail=100 api >&2
  exit 1
fi

api_port="$api_host_port"
api_health="$(curl --noproxy '*' -fsS --connect-timeout 2 --max-time 5 \
  "http://localhost:${api_port}/health")"
echo "Astra API is ready: http://localhost:${api_port}/health"
echo "$api_health"
echo "Terminal-Bench API URL: http://host.docker.internal:${api_port}"
