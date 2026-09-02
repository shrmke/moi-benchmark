#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Usage: run_dsh_incomplete_recovery_4.sh [--max-tasks N] [--retry-delay-seconds N] [--toolathlon-source PATH] OUTPUT_ROOT

Rerun the four tasks left incomplete by the six-task recovery batch. Tasks run
serially, get at most two attempts, and continue after a task remains incomplete.
Reuse OUTPUT_ROOT to resume.
EOF
}

max_tasks=4
retry_delay_seconds=60
source_root="${TOOLATHLON_SOURCE_ROOT:-/home/vagrant/dataset/Toolathlon}"
output_root=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --max-tasks)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --max-tasks requires a value." >&2
        exit 64
      fi
      max_tasks="$2"
      shift 2
      ;;
    --retry-delay-seconds)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --retry-delay-seconds requires a value." >&2
        exit 64
      fi
      retry_delay_seconds="$2"
      shift 2
      ;;
    --toolathlon-source)
      if [[ $# -lt 2 ]]; then
        echo "ERROR: --toolathlon-source requires a value." >&2
        exit 64
      fi
      source_root="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 64
      ;;
    *)
      if [[ -n "$output_root" ]]; then
        echo "ERROR: only one OUTPUT_ROOT may be provided." >&2
        exit 64
      fi
      output_root="$1"
      shift
      ;;
  esac
done

if [[ $# -gt 0 ]]; then
  if [[ -n "$output_root" || $# -ne 1 ]]; then
    echo "ERROR: only one OUTPUT_ROOT may be provided." >&2
    exit 64
  fi
  output_root="$1"
fi
if [[ -z "$output_root" ]]; then
  usage >&2
  exit 64
fi
if [[ ! "$max_tasks" =~ ^[1-4]$ ]]; then
  echo "ERROR: --max-tasks must be an integer from 1 through 4." >&2
  exit 64
fi
if [[ ! "$retry_delay_seconds" =~ ^[0-9]+$ ]]; then
  echo "ERROR: --retry-delay-seconds must be a non-negative integer." >&2
  exit 64
fi

# original_position failure_class task_id
task_specs=(
  "038 preprocess filter-low-selling-products"
  "084 preprocess sla-timeout-monitor"
  "089 preprocess task-tracker"
  "099 preprocess vlm-history-completer"
)

script_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root="${TOOLATHLON_REPO_ROOT:-$(cd -- "${script_root}/../../../.." && pwd)}"
source_root=$(readlink -m -- "$source_root")
output_root=$(readlink -m -- "$output_root")

if [[ ! -d "$source_root" ]]; then
  echo "ERROR: Toolathlon source is unavailable: $source_root" >&2
  exit 78
fi
for variable in TOOLATHLON_DSH_NODE TOOLATHLON_DSH_ROOT TOOLATHLON_DEEPSEEK_DSH_API_KEY TOOLATHLON_DSH_CREDENTIAL_MANIFEST; do
  if [[ -z ${!variable:-} ]]; then
    echo "ERROR: required environment variable is absent: $variable" >&2
    exit 78
  fi
done
if [[ ! -s "$TOOLATHLON_DSH_CREDENTIAL_MANIFEST" ]]; then
  echo "ERROR: credential manifest is unavailable: $TOOLATHLON_DSH_CREDENTIAL_MANIFEST" >&2
  exit 78
fi

if ! mkdir -p -- "$output_root"; then
  echo "ERROR: cannot create output root: $output_root" >&2
  exit 73
fi
if ! exec 9>"${output_root}/.dsh-incomplete-recovery-4.lock"; then
  echo "ERROR: cannot write to output root: $output_root" >&2
  exit 73
fi
if ! flock -n 9; then
  echo "ERROR: another DSH rerun holds ${output_root}/.dsh-incomplete-recovery-4.lock." >&2
  exit 75
fi
cd -- "$repo_root"
runs_root="${output_root}/runs/dsh"
if ! mkdir -p -- "$runs_root"; then
  echo "ERROR: cannot create runs root: $runs_root" >&2
  exit 73
fi

attempt_complete() {
  python3 - "$1" "$2" "$3" <<'PY'
import json
import sys
from pathlib import Path

root, task_id, run_id = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
try:
    run = json.loads((root / "run.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
required = (
    "artifacts.sha256",
    "model-usage.jsonl",
    "tool-calls.jsonl",
    "resource-usage.jsonl",
)
complete = (
    run.get("system_id") == "dsh"
    and run.get("task_id") == task_id
    and run.get("run_id") == run_id
    and run.get("artifact_gate", {}).get("status") == "passed"
    and run.get("run_validity") == "valid"
    and all((root / name).is_file() for name in required)
)
raise SystemExit(0 if complete else 1)
PY
}

completed=0
incomplete=0
for ((index = 0; index < max_tasks; index++)); do
  rerun_position=$((index + 1))
  read -r original_position failure_class task_id <<<"${task_specs[$index]}"
  task_root="${runs_root}/${task_id}"
  run_prefix="dsh-incomplete4-${original_position}-${task_id}"
  a1_id="${run_prefix}-a1"
  a2_id="${run_prefix}-a2"
  a1_dir="${task_root}/${a1_id}"
  a2_dir="${task_root}/${a2_id}"
  mkdir -p -- "$task_root"

  while true; do
    if attempt_complete "$a1_dir" "$task_id" "$a1_id" || \
       attempt_complete "$a2_dir" "$task_id" "$a2_id"; then
      echo "[$rerun_position/$max_tasks] SKIP complete: $task_id"
      completed=$((completed + 1))
      break
    fi

    if [[ ! -e "$a1_dir" ]]; then
      ordinal=1
      run_id="$a1_id"
      run_dir="$a1_dir"
      replacement_args=()
    elif [[ ! -e "$a2_dir" ]]; then
      ordinal=2
      run_id="$a2_id"
      run_dir="$a2_dir"
      replacement_args=(--replacement-for-run-id "$a1_id")
      echo "[$rerun_position/$max_tasks] preserving a1 and using a2: $task_id" >&2
    else
      echo "[$rerun_position/$max_tasks] SKIP incomplete after a1 and a2: $task_id" >&2
      incomplete=$((incomplete + 1))
      break
    fi

    echo "[$rerun_position/$max_tasks] START $task_id (a$ordinal; original=$original_position; source=$failure_class)"
    python3 -m astra.runners.toolathlon_dsh.lifecycle \
      --task-id "$task_id" \
      --run-id "$run_id" \
      --output-dir "$run_dir" \
      --toolathlon-source "$source_root" \
      "${replacement_args[@]}"
    status=$?
    if attempt_complete "$run_dir" "$task_id" "$run_id"; then
      echo "[$rerun_position/$max_tasks] RECORDED $task_id (process exit $status)"
      completed=$((completed + 1))
      break
    fi
    if [[ $ordinal -eq 1 ]]; then
      echo "[$rerun_position/$max_tasks] a1 is incomplete; waiting ${retry_delay_seconds}s before a2." >&2
      sleep "$retry_delay_seconds"
      continue
    fi
    echo "[$rerun_position/$max_tasks] INCOMPLETE $task_id after a2 (process exit $status); continuing." >&2
    incomplete=$((incomplete + 1))
    break
  done
done

echo "DSH rerun results: $output_root"
echo "Recorded selected tasks: $completed/$max_tasks (selected schedule: 4)"
echo "Incomplete selected tasks: $incomplete/$max_tasks"
if (( incomplete > 0 )); then
  exit 1
fi
