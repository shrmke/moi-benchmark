#!/usr/bin/env bash

set -euo pipefail

usage() {
  cat <<'EOF'
Run pending Astra Terminal-Bench 2.1 shard-4 cases with Pi's resource scheduler.

Usage:
  astra-terminal-bench-shard4-pending.sh [--check] [--yes]
      [--concurrency N] [--jobs-dir PATH] [--run-name NAME]

Options:
  --check          Print the pending cohort and Harbor's resolved config only.
  --yes            Start all pending cases.
  --concurrency N  Maximum Harbor worker processes (default: 3).
  --jobs-dir PATH  Result root (default: work/astra-glm52-c0-shard4-jobs).
  --run-name NAME  Batch/job-name prefix.
  -h, --help       Show this help.

A case is complete only when its latest matching attempt has a binary reward and
a verifier/ctrf.json report containing at least one executed test.
EOF
}

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd "$script_dir/../../.." && pwd)"
data_root="${MOI_BENCH_DATA_ROOT:-$workspace_root}"
shard_file="$workspace_root/astra/runners/astra_terminal_bench/tbench-2.1-shard-4.txt"
tasks_dir="$data_root/work/terminal-bench-2-1/tasks"
jobs_dir="$data_root/work/astra-glm52-c0-shard4-jobs"
resource_queue="$workspace_root/astra/runners/pi_terminal_bench/prebuilt/resource_queue.py"
schedule="$workspace_root/astra/runners/pi_terminal_bench/prebuilt/schedule.py"
runner="$script_dir/astra-terminal-bench-all-c0.sh"
config="$workspace_root/astra/runners/astra_terminal_bench/c0-cases-glm52.yaml"
concurrency=3
check_only=false
assume_yes=false
run_name=""

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
    --concurrency)
      [[ $# -ge 2 ]] || { echo "--concurrency requires a value" >&2; exit 2; }
      concurrency="$2"
      shift 2
      ;;
    --jobs-dir)
      [[ $# -ge 2 ]] || { echo "--jobs-dir requires a value" >&2; exit 2; }
      jobs_dir="$2"
      shift 2
      ;;
    --run-name)
      [[ $# -ge 2 ]] || { echo "--run-name requires a value" >&2; exit 2; }
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

[[ -f "$shard_file" ]] || { echo "missing shard file: $shard_file" >&2; exit 2; }
[[ -d "$tasks_dir" ]] || { echo "missing task directory: $tasks_dir" >&2; exit 2; }
[[ -x "$runner" ]] || { echo "missing Astra runner: $runner" >&2; exit 2; }
[[ -f "$resource_queue" && -f "$schedule" && -f "$config" ]] || {
  echo "missing Astra/Pi scheduler files" >&2
  exit 2
}

if ! [[ "$concurrency" =~ ^[1-9][0-9]*$ ]]; then
  echo "--concurrency must be a positive integer" >&2
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

harbor_shebang="$(head -n 1 "$harbor_bin" 2>/dev/null || true)"
python_bin="${harbor_shebang#\#!}"
if [[ ! -x "$python_bin" ]]; then
  python_bin="$(command -v python3)"
fi
"$python_bin" -c 'import tomllib' 2>/dev/null || {
  echo "Python 3.11+ is required to read Terminal-Bench task.toml files" >&2
  exit 2
}
shard_cases=()
while IFS= read -r case_name || [[ -n "$case_name" ]]; do
  case_name="${case_name%$'\r'}"
  [[ -z "$case_name" ]] || shard_cases+=("$case_name")
done < "$shard_file"
if [[ "${#shard_cases[@]}" -eq 0 ]]; then
  echo "shard file contains no cases: $shard_file" >&2
  exit 2
fi
if [[ "$(printf '%s\n' "${shard_cases[@]}" | sort -u | wc -l | tr -d '[:space:]')" != "${#shard_cases[@]}" ]]; then
  echo "shard file contains duplicate cases: $shard_file" >&2
  exit 2
fi

queue_path="$(mktemp "${TMPDIR:-/tmp}/astra-shard4-all.XXXXXX")"
pending_path="$(mktemp "${TMPDIR:-/tmp}/astra-shard4-pending.XXXXXX")"
trap 'rm -f "$queue_path" "$pending_path"' EXIT

queue_args=(
  "$resource_queue"
  --tasks-root "$tasks_dir"
  --output "$queue_path"
  --no-default-exclusions
)
for case_name in "${shard_cases[@]}"; do
  queue_args+=(--include-task "$case_name")
done
"$python_bin" "${queue_args[@]}"

"$python_bin" "$schedule" \
  --queue "$queue_path" \
  --jobs-dir "$jobs_dir" \
  --tasks-root "$tasks_dir" \
  --config "$config" \
  --workspace-root "$workspace_root" \
  --harbor-bin "$harbor_bin" \
  --print-pending \
  --expected-agent "astra.runners.astra_terminal_bench.agent:AstraTerminalBenchC0Agent" \
  --ignore-model-name \
  --ignore-agent-version \
  --cohort-kwarg 'max_turns=null' \
  --cohort-kwarg 'turn_timeout_sec=27000' \
  --cohort-kwarg 'trigger_timeout_sec=27000' \
  --cohort-kwarg 'stream_transport_retries=2' \
  --cohort-kwarg 'product_timeout_multiplier=1.0' \
  > "$pending_path"

pending_count="$(wc -l < "$pending_path" | tr -d '[:space:]')"
echo "Shard file: $shard_file"
echo "Shard cases: ${#shard_cases[@]}"
echo "Valid completed cases: $((${#shard_cases[@]} - pending_count))"
echo "Pending cases: $pending_count"
cut -f 1 "$pending_path" | sed 's/^/  - /'

if [[ "$pending_count" == "0" ]]; then
  echo "All shard-4 cases have valid verifier results."
  exit 0
fi

runner_args=(
  --concurrency "$concurrency"
  --jobs-dir "$jobs_dir"
)
[[ -z "$run_name" ]] || runner_args+=(--run-name "$run_name")
[[ "$check_only" == false ]] || runner_args+=(--check)
[[ "$assume_yes" == false ]] || runner_args+=(--yes)
while IFS=$'\t' read -r case_name _; do
  [[ -z "$case_name" ]] || runner_args+=(--case "$case_name")
done < "$pending_path"

exec "$runner" "${runner_args[@]}"
