#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/../.." && pwd)
if [[ -f "$REPO_ROOT/.env" ]]; then
  set -a
  # Central local credential entry point; values are never echoed.
  . "$REPO_ROOT/.env"
  set +a
fi
DATA_DIR="$REPO_ROOT/.local-services/maxkb_local/data"
LOG_DIR="$REPO_ROOT/.local-services/maxkb_local/logs"
IMAGE_TAG="1panel/maxkb:v2.10.4-lts"
IMAGE_REF="1panel/maxkb@sha256:20205df1ba6eef4e4276e48c892038de72cf8618d1e1c1d50eb1f535d45dfedc"
EXPECTED_ID="sha256:20205df1ba6eef4e4276e48c892038de72cf8618d1e1c1d50eb1f535d45dfedc"
EXPECTED_REPO_DIGEST="$IMAGE_REF"
EXPECTED_ARCH="arm64"
CONTAINER_NAME="moi-maxkb-local"

die() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

verify_image() {
  local actual_id actual_arch repo_digests
  actual_id=$(docker image inspect "$IMAGE_TAG" --format '{{.Id}}' 2>/dev/null) || \
    die "missing local image $IMAGE_TAG"
  actual_arch=$(docker image inspect "$IMAGE_TAG" --format '{{.Architecture}}')
  repo_digests=$(docker image inspect "$IMAGE_TAG" --format '{{join .RepoDigests "\n"}}')
  [[ "$actual_id" == "$EXPECTED_ID" ]] || die "image ID mismatch: $actual_id"
  [[ "$actual_arch" == "$EXPECTED_ARCH" ]] || die "image architecture mismatch: $actual_arch"
  grep -Fxq "$EXPECTED_REPO_DIGEST" <<<"$repo_digests" || \
    die "repository digest mismatch"
  printf 'verified image=%s architecture=linux/%s digest=%s\n' \
    "$IMAGE_TAG" "$actual_arch" "${EXPECTED_REPO_DIGEST#*@}"
}

assert_start_safe() {
  local running_names all_names
  running_names=$(docker ps --format '{{.Names}}')
  all_names=$(docker ps -a --format '{{.Names}}')
  if grep -Eq '^moi_dify_local-' <<<"$running_names" && [[ "${MAXKB_ALLOW_DIFY_CONCURRENT:-0}" != "1" ]]; then
    die "Dify is still running; set MAXKB_ALLOW_DIFY_CONCURRENT=1 for the validated concurrent deployment"
  fi
  grep -Fxq 'moi-openxml-parser' <<<"$running_names" || die "moi-openxml-parser is not running"
  grep -Fxq 'matrixone' <<<"$running_names" || die "matrixone is not running"
  if grep -Fxq "$CONTAINER_NAME" <<<"$all_names"; then
    die "container $CONTAINER_NAME already exists; inspect it instead of replacing it"
  fi
  if docker ps --format '{{.Ports}}' | grep -Eq '(^|[.:])8090->|:8090-'; then
    die "Docker port 8090 is already published"
  fi
  if command -v lsof >/dev/null && lsof -nP -iTCP:8090 -sTCP:LISTEN >/dev/null 2>&1; then
    die "host port 8090 is already listening"
  fi
}

start_maxkb() {
  verify_image
  assert_start_safe
  mkdir -p "$DATA_DIR"
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    --platform linux/arm64 \
    --pull never \
    --shm-size=512m \
    -e MAXKB_LOCAL_MODEL_HOST_WORKER=4 \
    -p 127.0.0.1:8090:8080 \
    -v "$DATA_DIR:/opt/maxkb" \
    "$IMAGE_REF"
}

resume_maxkb() {
  verify_image
  local running_names container_image container_status
  running_names=$(docker ps --format '{{.Names}}')
  if grep -Eq '^moi_dify_local-' <<<"$running_names" && [[ "${MAXKB_ALLOW_DIFY_CONCURRENT:-0}" != "1" ]]; then
    die "Dify is still running; set MAXKB_ALLOW_DIFY_CONCURRENT=1 for the validated concurrent deployment"
  fi
  grep -Fxq 'moi-openxml-parser' <<<"$running_names" || die "moi-openxml-parser is not running"
  grep -Fxq 'matrixone' <<<"$running_names" || die "matrixone is not running"
  container_status=$(docker inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null) || \
    die "container $CONTAINER_NAME does not exist; use start"
  [[ "$container_status" == "exited" || "$container_status" == "created" ]] || \
    die "container $CONTAINER_NAME state is $container_status"
  container_image=$(docker inspect --format '{{.Config.Image}}' "$CONTAINER_NAME")
  [[ "$container_image" == "$IMAGE_REF" ]] || die "container image mismatch: $container_image"
  docker start "$CONTAINER_NAME"
}

stop_maxkb() {
  docker stop "$CONTAINER_NAME"
  printf 'stopped %s; persistent data retained at %s\n' "$CONTAINER_NAME" "$DATA_DIR"
}

discover_api() {
  local base_url stamp output_dir entry label candidate status found schema_file
  base_url=${MAXKB_BASE_URL:-http://127.0.0.1:8090}
  curl -fsS --max-time 10 "$base_url/admin/" >/dev/null || die "MaxKB admin UI is not reachable"
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  output_dir="$LOG_DIR/api-discovery-$stamp"
  mkdir -p "$output_dir"
  : >"$output_dir/http-status.tsv"
  found=0
  for entry in \
    admin:/admin/api-doc/schema/ \
    chat:/chat/api-doc/schema/ \
    root:/api-doc/schema/ \
    openapi:/openapi.json; do
    label=${entry%%:*}
    candidate=${entry#*:}
    schema_file="$output_dir/$label-openapi-schema.json"
    status=$(curl -sS --max-time 15 -o "$schema_file" -w '%{http_code}' \
      "$base_url$candidate" || true)
    printf '%s\t%s\n' "$status" "$candidate" >>"$output_dir/http-status.tsv"
    if [[ "$status" == "200" ]] && python3 -m json.tool "$schema_file" >/dev/null 2>&1; then
      printf '%s\n' "$candidate" >"$output_dir/$label-schema-url-path.txt"
      python3 - "$schema_file" >"$output_dir/$label-openapi-paths.txt" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    schema = json.load(handle)
for path in sorted(schema.get("paths", {})):
    print(path)
PY
      found=$((found + 1))
    else
      rm -f "$schema_file"
    fi
  done
  if (( found > 0 )); then
    printf 'discovered %s schema(s); artifacts=%s\n' "$found" "$output_dir"
    return
  fi
  die "no public OpenAPI schema found; use the published agent UI API-document URL (statuses: $output_dir/http-status.tsv)"
}

run_smoke() {
  local fixture
  printf '%s\n' \
    'capability baseline: ingest=partial direct_retrieval=unsupported native_qa=partial'
  : "${MAXKB_API_KEY:?set MAXKB_API_KEY in the local runtime env}"
  : "${MAXKB_OPENAI_BASE_URL:?set the instance-provided MAXKB_OPENAI_BASE_URL}"
  : "${MAXKB_OPENAI_PATH:=/chat/completions}"
  for fixture in \
    001-project-boundary.md \
    002-service-ports.md \
    003-run-policy.md; do
    [[ -f "$REPO_ROOT/local-rag-platforms/fixtures/smoke/$fixture" ]] || die "missing fixture $fixture"
  done
  curl -fsS --max-time 10 "${MAXKB_BASE_URL:-http://127.0.0.1:8090}/admin/" >/dev/null || \
    die "MaxKB admin UI is not reachable"
  cd "$REPO_ROOT"
  PYTHONPATH="$REPO_ROOT/local-rag-platforms/dify-rag-eval/src${PYTHONPATH:+:$PYTHONPATH}" \
  python3 -m dify_rag_eval local-smoke \
    --system maxkb_local \
    --api-key-env MAXKB_API_KEY \
    --native-base-url "$MAXKB_OPENAI_BASE_URL" \
    --native-path "$MAXKB_OPENAI_PATH" \
    --output "$LOG_DIR/smoke"
}

run_full_chain() {
  exec "$SCRIPT_DIR/full-chain.sh"
}

run_qianfan_embedding() {
  exec python3 "$SCRIPT_DIR/maxkb_qianfan_embedding.py" "$@"
}

run_maas_models() {
  exec python3 "$SCRIPT_DIR/maxkb_maas_models.py" "$@"
}

start_or_resume_maxkb() {
  local existing status base_url attempt
  existing=$(docker inspect --format '{{.State.Status}}' "$CONTAINER_NAME" 2>/dev/null || true)
  if [[ -n "$existing" ]]; then
    if [[ "$existing" == "running" ]]; then
      printf '%s is already running\n' "$CONTAINER_NAME"
    else
      resume_maxkb
    fi
  else
    start_maxkb
  fi

  local selected_provider
  selected_provider="${MAXKB_EMBEDDING_PROVIDER:-${COMPETITOR_EMBEDDING_PROVIDER:-}}"
  if [[ "$selected_provider" == "qianfan" ]]; then
    base_url=${MAXKB_BASE_URL:-http://127.0.0.1:8090}
    status=""
    for attempt in $(seq 1 90); do
      status=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$base_url/admin/" || true)
      [[ "$status" == "200" ]] && break
      sleep 2
    done
    [[ "$status" == "200" ]] || die "MaxKB did not become ready for Qianfan embedding registration"
    python3 "$SCRIPT_DIR/maxkb_refresh_admin_token.py"
    python3 "$SCRIPT_DIR/maxkb_qianfan_embedding.py" --execute register
  elif [[ "$selected_provider" == "maas" ]]; then
    base_url=${MAXKB_BASE_URL:-http://127.0.0.1:8090}
    status=""
    for attempt in $(seq 1 90); do
      status=$(curl -sS --max-time 5 -o /dev/null -w '%{http_code}' "$base_url/admin/" || true)
      [[ "$status" == "200" ]] && break
      sleep 2
    done
    [[ "$status" == "200" ]] || die "MaxKB did not become ready for MaaS model registration"
    python3 "$SCRIPT_DIR/maxkb_refresh_admin_token.py"
    python3 "$SCRIPT_DIR/maxkb_maas_models.py" register --execute --skip-provider-probe
  fi
}

case "${1:-}" in
  verify-image) verify_image ;;
  start) start_or_resume_maxkb ;;
  resume) resume_maxkb ;;
  stop) stop_maxkb ;;
  discover) discover_api ;;
  smoke) run_smoke ;;
  full-chain) run_full_chain ;;
  qianfan-embedding) shift; run_qianfan_embedding "$@" ;;
  maas-models) shift; run_maas_models "$@" ;;
  *)
    printf 'usage: %s {verify-image|start|resume|stop|discover|smoke|full-chain|qianfan-embedding|maas-models}\n' "$0" >&2
    exit 2
    ;;
esac
