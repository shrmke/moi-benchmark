#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../../.." && pwd)"
export MOI_BENCH_ROOT="${MOI_BENCH_ROOT:-${REPO_ROOT}}"
DEFAULT_CONFIG="$(cd "${SCRIPT_DIR}/.." && pwd)/c0-four-cases-prebuilt.yaml"

TASKS_ROOT="${HERMES_TBENCH_TASKS_ROOT:-${REPO_ROOT}/work/terminal-bench-2-1/tasks}"
GENERATED_TASKS_ROOT="${HERMES_TBENCH_GENERATED_TASKS_ROOT:-${REPO_ROOT}/work/terminal-bench-2-1-hermes-prebuilt/tasks}"
CONFIG_PATH="${HERMES_TBENCH_CONFIG:-${DEFAULT_CONFIG}}"
IMAGE_PREFIX="${HERMES_PREBUILT_IMAGE_PREFIX:-moi/hermes-tbench}"
IMAGE_TAG="${HERMES_PREBUILT_IMAGE_TAG:-v2026.7.20}"
RUNTIME_IMAGE="${HERMES_PREBUILT_RUNTIME_IMAGE:-${IMAGE_PREFIX}-runtime:${IMAGE_TAG}}"
RUNTIME_BASE_IMAGE="${HERMES_PREBUILT_RUNTIME_BASE_IMAGE:-alexgshaw/modernize-scientific-stack:20251031}"
HARBOR_BIN="${HARBOR_BIN:-harbor}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
QUEUE_LOCK_DIR="${HERMES_PREBUILT_LOCK_DIR:-${REPO_ROOT}/work/.hermes-prebuilt-image-queue.lock}"
TEMPERATURE_CONFIGURATOR="${SCRIPT_DIR}/configure_temperature.py"
TEMPERATURE="0.0"
TEMPERATURE_SCOPE="primary_zai_chat_completions"
TEMPERATURE_PATCH_SHA256="6b71f1395a6533af731c506ceaed3dab885b04055bd3bc05eae696ba9786339a"
TEMPERATURE_PATCHED_SOURCE_SHA256="766e0fbb7b257701323bc4a4b49697047b1b20b46f6df53d85d48314516cca0a"
TEMPERATURE_CONFIGURATOR_SHA256=""
TASK_LAYOUT="terminal-only-v2"
BUILD_PROXY="${HERMES_TBENCH_BUILD_PROXY:-}"
BUILD_NETWORK_ARGS=()
BUILD_PROXY_ARGS=()
if [[ -n "${BUILD_PROXY}" ]]; then
  BUILD_NETWORK_ARGS+=(--network host)
  BUILD_PROXY_ARGS+=(
    --build-arg "HTTP_PROXY=${BUILD_PROXY}"
    --build-arg "HTTPS_PROXY=${BUILD_PROXY}"
    --build-arg "http_proxy=${BUILD_PROXY}"
    --build-arg "https_proxy=${BUILD_PROXY}"
    --build-arg "NO_PROXY=localhost,127.0.0.1,::1"
    --build-arg "no_proxy=localhost,127.0.0.1,::1"
  )
fi

PRINT_QUEUE=false
INSTALL_ONLY=false
REBUILD_RUNTIME=false
BUILD_ONLY=false
KEEP_IMAGES=false
LOCK_ACQUIRED=false
ACTIVE_CHILD_PID=""
PENDING_SIGNAL=""
CURRENT_IMAGE=""
CURRENT_IMAGE_ID=""
CURRENT_TASK=""
TASK_NAMES=()
FAILED_TASKS=()

usage() {
  sed -n 's/^#> //p' "$0"
}

#> Usage:
#>   build-images.sh [OPTIONS] TASK [TASK ...]
#>   build-images.sh [OPTIONS] --queue-file FILE
#>
#> Build and optionally run ephemeral Hermes task images.
#> At least one explicit task or queue file is required.
#>
#> Options:
#>   --queue-file FILE      Read one task name per line; blank lines and # comments are ignored.
#>   --config FILE          Harbor config (default: c0-four-cases-prebuilt.yaml).
#>   --tasks-root DIR       Source Terminal-Bench tasks directory.
#>   --generated-root DIR   Generated prebuilt task copies directory.
#>   --install-only         Run Harbor setup validation without submitting the task.
#>   --rebuild-runtime      Rebuild the shared Hermes runtime before processing the queue.
#>   --build-only          Build generated task images without running Harbor.
#>   --keep-images         Retain managed task images after successful processing.
#>   --print-queue          Validate input and print the de-duplicated queue without Docker.
#>   -h, --help             Show this help.
#>
#> The shared runtime and original task images are retained. By default every
#> managed task-derived image is deleted after its Harbor run. The Linux
#> runner uses --build-only --keep-images, then removes exact labelled images
#> after its resource-aware parallel schedule finishes.

die() {
  printf 'error: %s\n' "$*" >&2
  exit 2
}

add_task() {
  local candidate="$1"
  local existing

  candidate="${candidate#"${candidate%%[![:space:]]*}"}"
  candidate="${candidate%"${candidate##*[![:space:]]}"}"
  [[ -n "${candidate}" ]] || return 0
  [[ "${candidate}" =~ ^[A-Za-z0-9._-]+$ ]] \
    || die "invalid task name: ${candidate}"
  for existing in "${TASK_NAMES[@]:-}"; do
    [[ "${existing}" == "${candidate}" ]] && return 0
  done
  TASK_NAMES[${#TASK_NAMES[@]}]="${candidate}"
}

add_queue_file() {
  local queue_file="$1"
  local line

  [[ -f "${queue_file}" ]] || die "queue file not found: ${queue_file}"
  while IFS= read -r line || [[ -n "${line}" ]]; do
    line="${line%%#*}"
    add_task "${line}"
  done < "${queue_file}"
}

task_image() {
  printf '%s-%s:%s\n' "${IMAGE_PREFIX}" "$1" "${IMAGE_TAG}"
}

base_image_for_task() {
  local task_name="$1"
  local task_toml="${TASKS_ROOT}/${task_name}/task.toml"
  local images
  local count

  [[ -f "${task_toml}" ]] || return 1
  images="$(
    sed -nE \
      's/^[[:space:]]*docker_image[[:space:]]*=[[:space:]]*"([^"]+)".*$/\1/p' \
      "${task_toml}"
  )"
  count="$(printf '%s\n' "${images}" | sed '/^$/d' | wc -l | tr -d ' ')"
  [[ "${count}" == "1" ]] || return 1
  printf '%s\n' "${images}"
}

validate_task() {
  local task_name="$1"
  local base_image

  base_image="$(base_image_for_task "${task_name}")" \
    || die "task must contain exactly one docker_image: ${task_name}"
  [[ -n "${base_image}" ]] || die "empty docker_image for task: ${task_name}"
  [[ "${base_image}" != "${RUNTIME_IMAGE}" ]] || die \
    "task base image cannot be the shared Hermes runtime: ${task_name}"
  case "${base_image}" in
    "${IMAGE_PREFIX}-${task_name}:"*)
      die "tasks-root points to a generated Hermes task image: ${task_name}"
      ;;
  esac
}

image_label() {
  local image="$1"
  local label="$2"

  docker image inspect \
    --format "{{ index .Config.Labels \"${label}\" }}" \
    "${image}" 2>/dev/null
}

image_exists() {
  local image="$1"
  local output

  if output="$(
    docker image inspect --format '{{.Id}}' "${image}" 2>&1
  )"; then
    return 0
  fi
  case "${output}" in
    *"No such image:"*|*"No such object:"*)
      return 1
      ;;
    *)
      printf \
        'error: unable to inspect image %s: %s\n' \
        "${image}" "${output:-unknown Docker error}" \
        >&2
      return 2
      ;;
  esac
}

run_child() {
  local child_status=0

  PENDING_SIGNAL=""
  trap 'PENDING_SIGNAL=INT' INT
  trap 'PENDING_SIGNAL=TERM' TERM
  "$@" &
  ACTIVE_CHILD_PID=$!
  trap 'on_signal INT 130' INT
  trap 'on_signal TERM 143' TERM
  if [[ "${PENDING_SIGNAL}" == "INT" ]]; then
    on_signal INT 130
  elif [[ "${PENDING_SIGNAL}" == "TERM" ]]; then
    on_signal TERM 143
  fi
  wait "${ACTIVE_CHILD_PID}" || child_status=$?
  ACTIVE_CHILD_PID=""
  return "${child_status}"
}

cleanup_current_image() {
  local exists_status
  local kind
  local labelled_task
  local observed_id

  [[ -n "${CURRENT_IMAGE}" ]] || return 0
  if image_exists "${CURRENT_IMAGE}"; then
    :
  else
    exists_status=$?
    if [[ "${exists_status}" -eq 1 ]]; then
      CURRENT_IMAGE=""
      CURRENT_IMAGE_ID=""
      CURRENT_TASK=""
      return 0
    fi
    return "${exists_status}"
  fi
  kind="$(image_label "${CURRENT_IMAGE}" "io.moi.hermes-tbench.kind" || true)"
  labelled_task="$(
    image_label "${CURRENT_IMAGE}" "io.moi.hermes-tbench.task" || true
  )"
  observed_id="$(
    docker image inspect --format '{{.Id}}' "${CURRENT_IMAGE}" 2>/dev/null \
      || true
  )"
  if [[ "${kind}" != "ephemeral-task" || "${labelled_task}" != "${CURRENT_TASK}" ]]; then
    printf \
      'warning: refusing to delete unrecognized image %s (kind=%s task=%s)\n' \
      "${CURRENT_IMAGE}" "${kind:-<missing>}" "${labelled_task:-<missing>}" \
      >&2
    return 1
  fi
  if [[ -n "${CURRENT_IMAGE_ID}" \
    && "${observed_id}" != "${CURRENT_IMAGE_ID}" ]]; then
    printf \
      'warning: refusing to delete replaced image %s (expected=%s actual=%s)\n' \
      "${CURRENT_IMAGE}" "${CURRENT_IMAGE_ID}" "${observed_id:-<missing>}" \
      >&2
    return 1
  fi
  printf 'Removing ephemeral task image: %s\n' "${CURRENT_IMAGE}"
  if ! docker image rm --force "${CURRENT_IMAGE}" >/dev/null; then
    return 1
  fi
  CURRENT_IMAGE=""
  CURRENT_IMAGE_ID=""
  CURRENT_TASK=""
}

acquire_queue_lock() {
  mkdir -p "$(dirname "${QUEUE_LOCK_DIR}")"
  if ! mkdir "${QUEUE_LOCK_DIR}" 2>/dev/null; then
    die \
      "another Hermes image queue may be active; lock exists: ${QUEUE_LOCK_DIR}"
  fi
  LOCK_ACQUIRED=true
}

release_queue_lock() {
  [[ "${LOCK_ACQUIRED}" == "true" ]] || return 0
  if ! rmdir "${QUEUE_LOCK_DIR}"; then
    printf 'error: unable to release queue lock: %s\n' "${QUEUE_LOCK_DIR}" >&2
    return 1
  fi
  LOCK_ACQUIRED=false
}

on_exit() {
  trap '' INT TERM
  local exit_status="$1"

  trap - EXIT
  if ! cleanup_current_image; then
    [[ "${exit_status}" -ne 0 ]] || exit_status=1
  fi
  if ! release_queue_lock; then
    [[ "${exit_status}" -ne 0 ]] || exit_status=1
  fi
  exit "${exit_status}"
}

on_signal() {
  trap '' INT TERM
  local signal_name="$1"
  local exit_status="$2"
  local child_pid="${ACTIVE_CHILD_PID}"
  local attempts=0

  if [[ -n "${child_pid}" ]]; then
    kill "-${signal_name}" "${child_pid}" 2>/dev/null || true
    if [[ "${signal_name}" == "INT" ]]; then
      kill -TERM "${child_pid}" 2>/dev/null || true
    fi
    while kill -0 "${child_pid}" 2>/dev/null; do
      if [[ "${attempts}" -ge 100 ]]; then
        kill -KILL "${child_pid}" 2>/dev/null || true
        break
      fi
      sleep 0.1
      attempts=$((attempts + 1))
    done
    wait "${child_pid}" 2>/dev/null || true
    ACTIVE_CHILD_PID=""
  fi
  exit "${exit_status}"
}

trap 'on_exit $?' EXIT
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM

ensure_supported_builder() {
  local driver
  local inspection

  inspection="$(docker buildx inspect 2>/dev/null)" \
    || die "unable to inspect the active buildx builder"
  driver="$(
    printf '%s\n' "${inspection}" \
      | sed -nE 's/^Driver:[[:space:]]*//p'
  )"
  [[ "${driver}" == "docker" ]] || die \
    "active buildx driver must be docker so task builds can read local runtime image; got: ${driver:-unknown}"
}

ensure_runtime() {
  local exists_status
  local kind=""
  local revision=""
  local temperature=""
  local temperature_scope=""
  local temperature_patch_sha256=""
  local temperature_patched_source_sha256=""
  local temperature_configurator_sha256=""
  local should_build=false

  if image_exists "${RUNTIME_IMAGE}"; then
    kind="$(image_label "${RUNTIME_IMAGE}" "io.moi.hermes-tbench.kind" || true)"
    revision="$(
      image_label "${RUNTIME_IMAGE}" "org.opencontainers.image.revision" \
        || true
    )"
    temperature="$(
      image_label "${RUNTIME_IMAGE}" \
        "io.moi.hermes-tbench.temperature" || true
    )"
    temperature_scope="$(
      image_label "${RUNTIME_IMAGE}" \
        "io.moi.hermes-tbench.temperature-scope" || true
    )"
    temperature_patch_sha256="$(
      image_label "${RUNTIME_IMAGE}" \
        "io.moi.hermes-tbench.temperature-patch-sha256" || true
    )"
    temperature_patched_source_sha256="$(
      image_label "${RUNTIME_IMAGE}" \
        "io.moi.hermes-tbench.temperature-patched-source-sha256" || true
    )"
    temperature_configurator_sha256="$(
      image_label "${RUNTIME_IMAGE}" \
        "io.moi.hermes-tbench.temperature-configurator-sha256" || true
    )"
    [[ "${kind}" == "runtime" ]] || die \
      "refusing to overwrite unrecognized runtime image: ${RUNTIME_IMAGE}"
    if [[ "${REBUILD_RUNTIME}" == "true" \
      || "${revision}" != "3ef6bbd201263d354fd83ec55b3c306ded2eb72a" \
      || "${temperature}" != "${TEMPERATURE}" \
      || "${temperature_scope}" != "${TEMPERATURE_SCOPE}" \
      || "${temperature_patch_sha256}" != "${TEMPERATURE_PATCH_SHA256}" \
      || "${temperature_patched_source_sha256}" \
        != "${TEMPERATURE_PATCHED_SOURCE_SHA256}" \
      || "${temperature_configurator_sha256}" \
        != "${TEMPERATURE_CONFIGURATOR_SHA256}" ]]; then
      printf 'Shared runtime metadata is stale; rebuilding %s\n' "${RUNTIME_IMAGE}"
      should_build=true
    else
      printf 'Reusing shared Hermes runtime: %s\n' "${RUNTIME_IMAGE}"
    fi
  else
    exists_status=$?
    if [[ "${exists_status}" -eq 1 ]]; then
      should_build=true
    else
      die "cannot determine whether runtime image exists: ${RUNTIME_IMAGE}"
    fi
  fi

  if [[ "${should_build}" == "true" ]]; then
    printf 'Building shared Hermes runtime once: %s\n' "${RUNTIME_IMAGE}"
    run_child docker buildx build \
      --load \
      --platform linux/amd64 \
      "${BUILD_NETWORK_ARGS[@]}" \
      "${BUILD_PROXY_ARGS[@]}" \
      --build-arg "RUNTIME_BASE_IMAGE=${RUNTIME_BASE_IMAGE}" \
      --build-arg "HERMES_TEMPERATURE=${TEMPERATURE}" \
      --build-arg \
        "HERMES_TEMPERATURE_CONFIGURATOR_SHA256=${TEMPERATURE_CONFIGURATOR_SHA256}" \
      --tag "${RUNTIME_IMAGE}" \
      --file "${SCRIPT_DIR}/Dockerfile" \
      "${SCRIPT_DIR}"
  fi
}

verify_task_image() {
  local image="$1"
  local image_temperature
  local image_temperature_scope
  local image_temperature_patch_sha256
  local image_temperature_patched_source_sha256
  local image_configurator_sha256
  local image_layout

  image_temperature="$(
    image_label "${image}" "io.moi.hermes-tbench.temperature"
  )"
  image_configurator_sha256="$(
    image_label "${image}" \
      "io.moi.hermes-tbench.temperature-configurator-sha256"
  )"
  image_temperature_scope="$(
    image_label "${image}" "io.moi.hermes-tbench.temperature-scope"
  )"
  image_temperature_patch_sha256="$(
    image_label "${image}" "io.moi.hermes-tbench.temperature-patch-sha256"
  )"
  image_temperature_patched_source_sha256="$(
    image_label "${image}" \
      "io.moi.hermes-tbench.temperature-patched-source-sha256"
  )"
  image_layout="$(
    image_label "${image}" "io.moi.hermes-tbench.layout"
  )"
  [[ "${image_layout}" == "${TASK_LAYOUT}" ]] || return 1
  [[ "${image_temperature}" == "${TEMPERATURE}" ]] || return 1
  [[ "${image_temperature_scope}" == "${TEMPERATURE_SCOPE}" ]] || return 1
  [[ "${image_temperature_patch_sha256}" \
    == "${TEMPERATURE_PATCH_SHA256}" ]] || return 1
  [[ "${image_temperature_patched_source_sha256}" \
    == "${TEMPERATURE_PATCHED_SOURCE_SHA256}" ]] || return 1
  [[ "${image_configurator_sha256}" \
    == "${TEMPERATURE_CONFIGURATOR_SHA256}" ]] || return 1

  run_child docker run \
    --rm \
    --platform linux/amd64 \
    --entrypoint /bin/sh \
    "${image}" \
    -lc '
      set -eu
      test "$(cat /usr/local/lib/hermes-agent/.git/HEAD)" \
        = "3ef6bbd201263d354fd83ec55b3c306ded2eb72a"
      python_real="$(readlink -f \
        /usr/local/lib/hermes-agent/venv/bin/python)"
      case "${python_real}" in
        /usr/local/share/uv/python/*) ;;
        *)
          printf "Hermes venv is not relocatable: %s\n" \
            "${python_real}" >&2
          exit 1
          ;;
      esac
      test -x "${python_real}"
      test -x "$(command -v python3)"
      expected_temperature="$1"
      expected_scope="$2"
      expected_patch_sha256="$3"
      expected_patched_source_sha256="$4"
      expected_configurator_sha256="$5"
      test "$(cat /opt/moi/hermes-temperature)" \
        = "${expected_temperature}"
      actual_temperature="$(cd /usr/local/lib/hermes-agent \
        && venv/bin/python -c \
        '"'"'from providers import get_provider_profile; print(get_provider_profile("zai").fixed_temperature)'"'"')"
      test "${actual_temperature}" = "${expected_temperature}"
      python3 -c \
        '"'"'import hashlib,json,sys; from pathlib import Path; audit=json.load(open("/opt/moi/hermes-temperature.json")); marker=json.load(open("/opt/moi/hermes-preinstalled.json")); source=Path("/usr/local/lib/hermes-agent/plugins/model-providers/zai/__init__.py").read_bytes(); patch=Path("/opt/moi/hermes-temperature.patch").read_bytes(); assert audit["temperature"] == float(sys.argv[1]) == 0.0; assert audit["scope"] == sys.argv[2]; assert hashlib.sha256(source).hexdigest() == audit["patched_source_sha256"] == sys.argv[4]; assert hashlib.sha256(patch).hexdigest() == audit["patch_sha256"] == sys.argv[3]; assert marker["temperature"] == audit["temperature"]; assert marker["temperature_scope"] == audit["scope"]; assert marker["temperature_patch_sha256"] == audit["patch_sha256"]; assert marker["temperature_patched_source_sha256"] == audit["patched_source_sha256"]; assert marker["temperature_configurator_sha256"] == sys.argv[5]'"'"' \
        "${expected_temperature}" \
        "${expected_scope}" \
        "${expected_patch_sha256}" \
        "${expected_patched_source_sha256}" \
        "${expected_configurator_sha256}"
      /usr/local/bin/hermes version \
        | grep -F "Hermes Agent v0.19.0 (2026.7.20)"
      test "$(readlink -f /usr/local/bin/node)" \
        = "/root/.hermes/node/bin/node"
    ' verify \
      "${TEMPERATURE}" \
      "${TEMPERATURE_SCOPE}" \
      "${TEMPERATURE_PATCH_SHA256}" \
      "${TEMPERATURE_PATCHED_SOURCE_SHA256}" \
      "${TEMPERATURE_CONFIGURATOR_SHA256}"
}

build_task_image() {
  local task_name="$1"
  local base_image
  local image
  local exists_status
  local existing_kind=""
  local existing_task=""
  local existing_base=""
  local existing_layout=""

  base_image="$(base_image_for_task "${task_name}")" || return $?
  image="$(task_image "${task_name}")"
  CURRENT_TASK="${task_name}"
  CURRENT_IMAGE="${image}"
  CURRENT_IMAGE_ID=""

  if image_exists "${image}"; then
    existing_kind="$(
      image_label "${image}" "io.moi.hermes-tbench.kind" || true
    )"
    existing_task="$(
      image_label "${image}" "io.moi.hermes-tbench.task" || true
    )"
    if [[ "${existing_kind}" != "ephemeral-task" \
      || "${existing_task}" != "${task_name}" ]]; then
      die "refusing to overwrite unrecognized image: ${image}"
    fi
    existing_base="$(
      image_label "${image}" "io.moi.hermes-tbench.base-image" || true
    )"
    existing_layout="$(
      image_label "${image}" "io.moi.hermes-tbench.layout" || true
    )"
    if [[ "${existing_base}" == "${base_image}" \
      && "${existing_layout}" == "${TASK_LAYOUT}" ]] \
      && verify_task_image "${image}"; then
      CURRENT_IMAGE_ID="$(
        docker image inspect --format '{{.Id}}' "${image}"
      )" || return $?
      printf 'Reusing verified task image: %s\n' "${image}"
      return 0
    fi
    printf 'Removing stale managed task image: %s\n' "${image}"
    docker image rm --force "${image}" >/dev/null || return $?
  else
    exists_status=$?
    [[ "${exists_status}" -eq 1 ]] || return "${exists_status}"
  fi

  printf 'Building task image on demand: %s <- %s\n' "${image}" "${base_image}"
  run_child docker buildx build \
    --load \
    --platform linux/amd64 \
    "${BUILD_NETWORK_ARGS[@]}" \
    "${BUILD_PROXY_ARGS[@]}" \
    --build-arg "BASE_IMAGE=${base_image}" \
    --build-arg "HERMES_RUNTIME_IMAGE=${RUNTIME_IMAGE}" \
    --build-arg "HERMES_TEMPERATURE=${TEMPERATURE}" \
    --build-arg \
      "HERMES_TEMPERATURE_CONFIGURATOR_SHA256=${TEMPERATURE_CONFIGURATOR_SHA256}" \
    --build-arg "HERMES_TASK_LAYOUT=${TASK_LAYOUT}" \
    --build-arg "TASK_NAME=${task_name}" \
    --tag "${image}" \
    --file "${SCRIPT_DIR}/Dockerfile.task" \
    "${SCRIPT_DIR}" \
    || return $?
  CURRENT_IMAGE_ID="$(
    docker image inspect --format '{{.Id}}' "${image}"
  )" || return $?
  verify_task_image "${image}" || return $?
}

prepare_generated_task() {
  local task_name="$1"

  run_child "${PYTHON_BIN}" "${SCRIPT_DIR}/prepare_tasks.py" \
    --source "${TASKS_ROOT}" \
    --destination "${GENERATED_TASKS_ROOT}" \
    --image-prefix "${IMAGE_PREFIX}" \
    --image-tag "${IMAGE_TAG}" \
    --overwrite \
    "${task_name}"
}

run_task() {
  local task_name="$1"
  local generated_task="${GENERATED_TASKS_ROOT}/${task_name}"
  local harbor_args=()

  harbor_args=(
    run
    --config "${CONFIG_PATH}"
    --path "${generated_task}"
    --no-force-build
    --yes
  )
  if [[ "${INSTALL_ONLY}" == "true" ]]; then
    harbor_args+=(--install-only)
  fi

  printf 'Running queued task: %s\n' "${task_name}"
  run_child env \
    "PYTHONPATH=${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}" \
    "${HARBOR_BIN}" "${harbor_args[@]}"
}

process_task() {
  local task_name="$1"
  local status=0

  printf '\n=== Queue task: %s ===\n' "${task_name}"
  build_task_image "${task_name}" || status=$?
  if [[ "${status}" -eq 0 ]]; then
    prepare_generated_task "${task_name}" || status=$?
  fi
  if [[ "${status}" -eq 0 && "${BUILD_ONLY}" == "false" ]]; then
    run_task "${task_name}" || status=$?
  fi

  if [[ "${status}" -eq 0 && "${KEEP_IMAGES}" == "true" ]]; then
    CURRENT_IMAGE=""
    CURRENT_IMAGE_ID=""
    CURRENT_TASK=""
  elif ! cleanup_current_image; then
    [[ "${status}" -ne 0 ]] || status=1
  fi
  return "${status}"
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --queue-file)
      [[ "$#" -ge 2 ]] || die "--queue-file requires a path"
      add_queue_file "$2"
      shift 2
      ;;
    --config)
      [[ "$#" -ge 2 ]] || die "--config requires a path"
      CONFIG_PATH="$2"
      shift 2
      ;;
    --tasks-root)
      [[ "$#" -ge 2 ]] || die "--tasks-root requires a path"
      TASKS_ROOT="$2"
      shift 2
      ;;
    --generated-root)
      [[ "$#" -ge 2 ]] || die "--generated-root requires a path"
      GENERATED_TASKS_ROOT="$2"
      shift 2
      ;;
    --install-only)
      INSTALL_ONLY=true
      shift
      ;;
    --rebuild-runtime)
      REBUILD_RUNTIME=true
      shift
      ;;
    --build-only)
      BUILD_ONLY=true
      shift
      ;;
    --keep-images)
      KEEP_IMAGES=true
      shift
      ;;
    --print-queue)
      PRINT_QUEUE=true
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      while [[ "$#" -gt 0 ]]; do
        add_task "$1"
        shift
      done
      ;;
    -*)
      die "unknown option: $1"
      ;;
    *)
      add_task "$1"
      shift
      ;;
  esac
done

[[ "${#TASK_NAMES[@]}" -gt 0 ]] || {
  usage >&2
  die "at least one task or --queue-file is required"
}

for task_name in "${TASK_NAMES[@]}"; do
  validate_task "${task_name}"
done

if [[ "${PRINT_QUEUE}" == "true" ]]; then
  printf '%s\n' "${TASK_NAMES[@]}"
  exit 0
fi

[[ -f "${CONFIG_PATH}" ]] || die "Harbor config not found: ${CONFIG_PATH}"
CONFIG_PATH="$(
  cd "$(dirname "${CONFIG_PATH}")"
  printf '%s/%s\n' "$(pwd)" "$(basename "${CONFIG_PATH}")"
)"
if [[ "${CONFIG_PATH}" == "${DEFAULT_CONFIG}" ]]; then
  for task_name in "${TASK_NAMES[@]}"; do
    case "${task_name}" in
      modernize-scientific-stack|overfull-hbox|build-pmars|db-wal-recovery)
        ;;
      *)
        die \
          "the default four-case config cannot run ${task_name}; pass --config ${SCRIPT_DIR}/../c0-all-prebuilt.yaml"
        ;;
    esac
  done
fi

command -v docker >/dev/null 2>&1 || die "docker is not available"
command -v "${HARBOR_BIN}" >/dev/null 2>&1 \
  || die "Harbor executable not found: ${HARBOR_BIN}"
command -v "${PYTHON_BIN}" >/dev/null 2>&1 \
  || die "Python executable not found: ${PYTHON_BIN}"
if [[ "${BUILD_ONLY}" == "false" ]]; then
  [[ -n "${GLM_API_KEY:-}" ]] || die "GLM_API_KEY is required"
fi
[[ -f "${TEMPERATURE_CONFIGURATOR}" ]] \
  || die "temperature configurator not found: ${TEMPERATURE_CONFIGURATOR}"
TEMPERATURE_CONFIGURATOR_SHA256="$(
  "${PYTHON_BIN}" -c \
    'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' \
    "${TEMPERATURE_CONFIGURATOR}"
)"

acquire_queue_lock
ensure_supported_builder
ensure_runtime

for task_name in "${TASK_NAMES[@]}"; do
  if ! process_task "${task_name}"; then
    FAILED_TASKS[${#FAILED_TASKS[@]}]="${task_name}"
    printf 'Task queue entry failed: %s\n' "${task_name}" >&2
    if [[ -n "${CURRENT_IMAGE}" ]]; then
      printf \
        'Aborting queue because ephemeral image cleanup failed: %s\n' \
        "${CURRENT_IMAGE}" \
        >&2
      break
    fi
  fi
done

if [[ "${#FAILED_TASKS[@]}" -gt 0 ]]; then
  printf 'Failed queue entries:' >&2
  printf ' %s' "${FAILED_TASKS[@]}" >&2
  printf '\n' >&2
  exit 1
fi

if [[ "${KEEP_IMAGES}" == "true" ]]; then
  printf 'Queue completed; managed task-derived images were retained.\n'
else
  printf 'Queue completed; no managed task-derived images were retained.\n'
fi
