#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Run selected Terminal-Bench 2.1 cases with Astra C0 and resource-aware workers.

Usage:
  astra-terminal-bench-all-c0.sh [--check] [--yes]
                                      [--case NAME]...
                                      [--concurrency N]
                                      [--jobs-dir PATH]
                                      [--run-name NAME]

Options:
  --check          Validate inputs and print Harbor's resolved config; do not run.
  --yes            Pass --yes to Harbor.
  --case NAME      Terminal-Bench case to run; repeat for multiple cases.
                   When omitted, all 89 cases are queued.
  --concurrency N  Maximum Harbor worker processes (default: 3).
  --jobs-dir PATH  Result root (default: work/astra-glm52-c0-cases-jobs).
  --run-name NAME  Batch/job-name prefix (default: astra-glm52-c0-YYYYMMDD-HHMMSS).
  -h, --help       Show this help.

Workers reuse Pi's 8GB-aware policy: three 2GB memory tokens and six CPUs.
An 8GB task runs alone, a 4GB task may pair with one 2GB task, and up to three
2GB tasks may overlap. Use a new run name for each reproduction.

Environment overrides:
  HARBOR_BIN
  MOI_BENCH_DATA_ROOT               Dataset/result root (default: repository root).
  ASTRA_API_URL
  ASTRA_TBENCH_LINUX_BINARY
  ASTRA_TBENCH_READ_MEMORY
  ASTRA_ACCESS_TOKEN                 Required only when memory reads are enabled.
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "$script_dir/../../.." && pwd)"
data_root="${MOI_BENCH_DATA_ROOT:-$workspace_root}"
config_path="$workspace_root/astra/runners/astra_terminal_bench/c0-cases-glm52.yaml"
resource_queue="$workspace_root/astra/runners/pi_terminal_bench/prebuilt/resource_queue.py"
schedule="$workspace_root/astra/runners/pi_terminal_bench/prebuilt/schedule.py"
astra_source_root="$workspace_root/external/astra-optimize_0731_05"
dataset_root="$data_root/work/terminal-bench-2-1"
tasks_dir="$dataset_root/tasks"
jobs_dir="$data_root/work/astra-glm52-c0-cases-jobs"
expected_dataset_commit="5c8eadf1f393183288fa08b8f73ca9a469cc5e00"
concurrency=3
check_only=false
assume_yes=false
run_name=""
cases=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --check)
      check_only=true
      shift
      ;;
    --yes)
      assume_yes=true
      shift
      ;;
    --case)
      [[ $# -ge 2 ]] || {
        echo "--case requires a value" >&2
        exit 2
      }
      cases+=("$2")
      shift 2
      ;;
    --concurrency)
      [[ $# -ge 2 ]] || {
        echo "--concurrency requires a value" >&2
        exit 2
      }
      concurrency="$2"
      shift 2
      ;;
    --jobs-dir)
      [[ $# -ge 2 ]] || {
        echo "--jobs-dir requires a value" >&2
        exit 2
      }
      jobs_dir="$2"
      shift 2
      ;;
    --run-name)
      [[ $# -ge 2 ]] || {
        echo "--run-name requires a value" >&2
        exit 2
      }
      run_name="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! [[ "$concurrency" =~ ^[1-9][0-9]*$ ]]; then
  echo "--concurrency must be a positive integer" >&2
  exit 2
fi
if [[ -z "$run_name" ]]; then
  run_name="astra-glm52-c0-$(date '+%Y%m%d-%H%M%S')"
fi
if ! [[ "$run_name" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "--run-name may contain only letters, digits, dot, underscore, and hyphen" >&2
  exit 2
fi

if [[ -n "${HARBOR_BIN:-}" ]]; then
  harbor_bin="$HARBOR_BIN"
elif command -v harbor >/dev/null 2>&1; then
  harbor_bin="$(command -v harbor)"
elif [[ -x "$HOME/.local/bin/harbor" ]]; then
  harbor_bin="$HOME/.local/bin/harbor"
else
  echo "harbor was not found; install Harbor 0.20.0 or set HARBOR_BIN" >&2
  exit 2
fi

harbor_version="$("$harbor_bin" --version 2>&1 | tail -n 1 | tr -d '[:space:]')"
if [[ "$harbor_version" != "0.20.0" ]]; then
  echo "this runner is pinned to Harbor 0.20.0; found: $harbor_version" >&2
  exit 2
fi
harbor_shebang="$(head -n 1 "$harbor_bin" 2>/dev/null || true)"
harbor_python="${harbor_shebang#\#!}"
if [[ ! -x "$harbor_python" ]]; then
  harbor_python="$(command -v python3)"
fi
"$harbor_python" -c \
  'from pathlib import Path; import sys; p=Path(sys.argv[1]); compile(p.read_text(encoding="utf-8"), str(p), "exec")' \
  "$workspace_root/astra/runners/llm_observability.py"

[[ -f "$config_path" ]] || {
  echo "missing Astra C0 config: $config_path" >&2
  exit 2
}
[[ -f "$resource_queue" && -f "$schedule" ]] || {
  echo "missing Pi resource scheduler" >&2
  exit 2
}
[[ -f "$astra_source_root/Cargo.toml" ]] || {
  echo "missing new Astra source checkout: $astra_source_root" >&2
  exit 2
}
[[ -d "$tasks_dir" ]] || {
  echo "missing Terminal-Bench task directory: $tasks_dir" >&2
  exit 2
}

actual_dataset_commit="$(git -C "$dataset_root" rev-parse HEAD 2>/dev/null)" || {
  echo "Terminal-Bench snapshot is not a readable Git checkout: $dataset_root" >&2
  exit 2
}
if [[ "$actual_dataset_commit" != "$expected_dataset_commit" ]]; then
  echo "unexpected Terminal-Bench commit: $actual_dataset_commit" >&2
  echo "expected: $expected_dataset_commit" >&2
  exit 2
fi
dataset_changes="$(
  git -C "$dataset_root" status --short --untracked-files=all -- tasks
)"
if [[ -n "$dataset_changes" ]]; then
  echo "Terminal-Bench task snapshot has local changes:" >&2
  printf '%s\n' "$dataset_changes" >&2
  exit 2
fi

task_count="$(
  find "$tasks_dir" -mindepth 2 -maxdepth 2 -type f -name task.toml -print |
    wc -l |
    tr -d '[:space:]'
)"
if [[ "$task_count" != "89" ]]; then
  echo "expected the pinned Terminal-Bench 2.1 snapshot with 89 tasks; found $task_count" >&2
  exit 2
fi

export ASTRA_API_URL="${ASTRA_API_URL:-http://host.docker.internal:17001}"
export ASTRA_TBENCH_LINUX_BINARY="${ASTRA_TBENCH_LINUX_BINARY:-$workspace_root/work/astra-optimize-0731-05-linux-amd64/target/release/astra}"
export ASTRA_TBENCH_MODEL="${ASTRA_TBENCH_MODEL:-glm-5.2(thinking:high)}"
export ASTRA_TBENCH_READ_MEMORY="${ASTRA_TBENCH_READ_MEMORY:-false}"
export ASTRA_TBENCH_TEMPERATURE="${ASTRA_TBENCH_TEMPERATURE:-0}"
export PYTHONPATH="$workspace_root${PYTHONPATH:+:$PYTHONPATH}"

[[ "$ASTRA_TBENCH_MODEL" == "glm-5.2(thinking:high)" ]] || {
  echo "this runner is fixed to glm-5.2(thinking:high); found: $ASTRA_TBENCH_MODEL" >&2
  exit 2
}
[[ "$ASTRA_TBENCH_TEMPERATURE" == "0" ]] || {
  echo "this runner is fixed to temperature=0; found: $ASTRA_TBENCH_TEMPERATURE" >&2
  exit 2
}

case "$ASTRA_API_URL" in
  http://host.docker.internal|http://host.docker.internal:*|https://host.docker.internal|https://host.docker.internal:*)
    ;;
  *)
    echo "ASTRA_API_URL must use host.docker.internal from Docker tasks" >&2
    exit 2
    ;;
esac

host_api_url="${ASTRA_API_URL/host.docker.internal/localhost}"
if ! curl --fail --silent --show-error --max-time 5 \
  "${host_api_url%/}/health" >/dev/null; then
  echo "Astra API health check failed: ${host_api_url%/}/health" >&2
  echo "start the Astra server before running Terminal-Bench" >&2
  exit 2
fi

[[ -f "$ASTRA_TBENCH_LINUX_BINARY" ]] || {
  echo "missing Linux Astra binary: $ASTRA_TBENCH_LINUX_BINARY" >&2
  exit 2
}
binary_description="$(file "$ASTRA_TBENCH_LINUX_BINARY")"
if [[ "$binary_description" != *"ELF 64-bit"* || "$binary_description" != *"x86-64"* ]]; then
  echo "the pinned Terminal-Bench images require an x86-64 Linux Astra ELF" >&2
  echo "$binary_description" >&2
  exit 2
fi

case "$ASTRA_TBENCH_READ_MEMORY" in
  true|TRUE|True|1|yes|YES|Yes|on|ON|On)
    if [[ -z "${ASTRA_ACCESS_TOKEN:-}" ]]; then
      echo "ASTRA_ACCESS_TOKEN is required when ASTRA_TBENCH_READ_MEMORY=true" >&2
      exit 2
    fi
    ;;
  false|FALSE|False|0|no|NO|No|off|OFF|Off)
    ;;
  *)
    echo "ASTRA_TBENCH_READ_MEMORY must be a boolean value" >&2
    exit 2
    ;;
esac

queue_path="$(mktemp "${TMPDIR:-/tmp}/astra-glm52-cases.XXXXXX")"
trap 'rm -f "$queue_path"' EXIT
queue_args=(
  "$resource_queue"
  --tasks-root "$tasks_dir"
  --output "$queue_path"
  --no-default-exclusions
)
for case_name in "${cases[@]}"; do
  queue_args+=(--include-task "$case_name")
done
"$harbor_python" "${queue_args[@]}"
selected_task_count="$(wc -l < "$queue_path" | tr -d '[:space:]')"
if [[ "$selected_task_count" == "0" ]]; then
  echo "no Terminal-Bench cases were selected" >&2
  exit 2
fi
first_task="$(head -n 1 "$queue_path" | cut -f 1)"
astra_source_commit="$(git -C "$astra_source_root" rev-parse HEAD)"
binary_sha256="$(shasum -a 256 "$ASTRA_TBENCH_LINUX_BINARY" | cut -d ' ' -f 1)"

echo "Harbor: $harbor_version"
echo "Workspace commit: $(git -C "$workspace_root" rev-parse HEAD)"
echo "Dataset commit: $actual_dataset_commit"
echo "Astra source: $astra_source_root"
echo "Astra source commit: $astra_source_commit"
echo "Condition: C0 (task-specific trigger when registered; generic product-live otherwise)"
echo "Selected tasks: $selected_task_count / $task_count"
cut -f 1 "$queue_path" | sed 's/^/  - /'
echo "Worker processes: at most $concurrency"
echo "Resource policy: 3 memory tokens, 6 CPUs; 8GB tasks run alone"
echo "Product timeout: each task's upstream [agent].timeout_sec x 1.0"
echo "Harbor agent phase timeout: upstream timeout x 2.5 (includes cleanup and trajectory)"
echo "LLM fallback timeout: 600 seconds"
echo "Stream transport retries: 2 (same Astra session)"
echo "Jobs directory: $jobs_dir"
echo "Run name: $run_name"
echo "Astra binary: $ASTRA_TBENCH_LINUX_BINARY"
echo "Astra binary SHA-256: $binary_sha256"
echo "Astra model: $ASTRA_TBENCH_MODEL"
echo "Thinking effort: high"
echo "Temperature requested: 0"
echo "Temperature effective: omitted (Astra thinking protocol forbids temperature)"
echo "Read existing user memory: $ASTRA_TBENCH_READ_MEMORY"

if [[ "$check_only" == true ]]; then
  if [[ -n "${ASTRA_ACCESS_TOKEN:-}" ]]; then
    ASTRA_ACCESS_TOKEN=redacted-for-config-check \
      "$harbor_bin" run \
        --config "$config_path" \
        --path "$tasks_dir/$first_task" \
        --jobs-dir "$jobs_dir" \
        --job-name "$run_name-$first_task" \
        --print-config
  else
    "$harbor_bin" run \
      --config "$config_path" \
      --path "$tasks_dir/$first_task" \
      --jobs-dir "$jobs_dir" \
      --job-name "$run_name-$first_task" \
      --print-config
  fi
  exit 0
fi

[[ "$assume_yes" == true ]] || {
  echo "pass --yes to start the selected cases" >&2
  exit 2
}

manifest_dir="$jobs_dir/.reproduction"
manifest_path="$manifest_dir/$run_name.tsv"
queue_manifest_path="$manifest_dir/$run_name.queue.tsv"
mkdir -p "$manifest_dir"
if [[ -e "$manifest_path" ]]; then
  echo "reproduction manifest already exists for run name: $run_name" >&2
  echo "choose a new --run-name" >&2
  exit 2
fi
workspace_commit="$(git -C "$workspace_root" rev-parse HEAD)"
workspace_tracked_state="clean"
git -C "$workspace_root" diff --quiet -- . || workspace_tracked_state="dirty"
git -C "$workspace_root" diff --cached --quiet -- . || workspace_tracked_state="dirty"
{
  printf 'schema_version\t1\n'
  printf 'run_name\t%s\n' "$run_name"
  printf 'workspace_commit\t%s\n' "$workspace_commit"
  printf 'workspace_tracked_state\t%s\n' "$workspace_tracked_state"
  printf 'dataset_commit\t%s\n' "$actual_dataset_commit"
  printf 'astra_source_root\t%s\n' "$astra_source_root"
  printf 'astra_source_commit\t%s\n' "$astra_source_commit"
  printf 'harbor_version\t%s\n' "$harbor_version"
  printf 'condition\tC0\n'
  printf 'lifecycle_audit_is_score_gate\tfalse\n'
  printf 'task_count\t%s\n' "$selected_task_count"
  printf 'worker_process_limit\t%s\n' "$concurrency"
  printf 'resource_policy\tpi_3_memory_tokens_6_cpus\n'
  printf 'queue_path\t%s\n' "$queue_manifest_path"
  printf 'jobs_dir\t%s\n' "$jobs_dir"
  printf 'astra_api_url\t%s\n' "$ASTRA_API_URL"
  printf 'astra_binary\t%s\n' "$ASTRA_TBENCH_LINUX_BINARY"
  printf 'astra_binary_description\t%s\n' "$binary_description"
  printf 'astra_binary_sha256\t%s\n' "$binary_sha256"
  printf 'astra_model\t%s\n' "$ASTRA_TBENCH_MODEL"
  printf 'thinking_effort\thigh\n'
  printf 'temperature_requested\t0\n'
  printf 'temperature_effective\tomitted_by_thinking_protocol\n'
  printf 'astra_read_memory\t%s\n' "$ASTRA_TBENCH_READ_MEMORY"
  printf 'product_timeout_policy\tupstream_agent_timeout_x_1.0\n'
  printf 'harbor_agent_timeout_policy\tupstream_agent_timeout_x_2.5\n'
  printf 'stream_transport_retries\t2\n'
} > "$manifest_path"
cp "$queue_path" "$queue_manifest_path"

set +e
"$harbor_python" "$schedule" \
  --queue "$queue_manifest_path" \
  --jobs-dir "$jobs_dir" \
  --tasks-root "$tasks_dir" \
  --config "$config_path" \
  --workspace-root "$workspace_root" \
  --harbor-bin "$harbor_bin" \
  --rerun-completed \
  --max-workers "$concurrency" \
  --job-name-prefix "$run_name"
run_status="$?"
set -e
printf 'scheduler_exit_code\t%s\n' "$run_status" >> "$manifest_path"
echo "Reproduction manifest: $manifest_path"
exit "$run_status"
