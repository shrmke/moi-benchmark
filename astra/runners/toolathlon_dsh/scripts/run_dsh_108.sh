#!/usr/bin/env bash
set -uo pipefail

usage() {
  cat <<'EOF'
Usage: run_dsh_108.sh [--max-tasks N] [--toolathlon-source PATH] OUTPUT_ROOT

Run the frozen Toolathlon task schedule serially with DSH. The default is all
108 tasks; --max-tasks selects the first N tasks. Reuse OUTPUT_ROOT to resume.
EOF
}

max_tasks=108
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
if [[ ! "$max_tasks" =~ ^[1-9][0-9]*$ ]] || (( max_tasks > 108 )); then
  echo "ERROR: --max-tasks must be an integer from 1 through 108." >&2
  exit 64
fi

script_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo_root="${TOOLATHLON_REPO_ROOT:-$(cd -- "${script_root}/../../../.." && pwd)}"
freeze_root="${repo_root}/astra/benchmark/toolathlon-verified/freeze"
protocol_path="${freeze_root}/execution-protocol.freeze.json"
requirements_path="${freeze_root}/task-requirements.json"
source_root=$(readlink -m -- "$source_root")
output_root=$(readlink -m -- "$output_root")

if [[ ! -d "$source_root" ]]; then
  echo "ERROR: Toolathlon source is unavailable: $source_root" >&2
  exit 78
fi
for variable in TOOLATHLON_DSH_NODE TOOLATHLON_DSH_ROOT TOOLATHLON_DEEPSEEK_DSH_API_KEY; do
  if [[ -z ${!variable:-} ]]; then
    echo "ERROR: required environment variable is absent: $variable" >&2
    exit 78
  fi
done

mapfile -t tasks < <(
  python3 - "$protocol_path" "$requirements_path" <<'PY'
import json
import sys
from pathlib import Path

protocol = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
requirements = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
phases = protocol.get("formal_phases", {})
tasks = list(phases.get("first_batch", {}).get("tasks", []))
tasks.extend(phases.get("remaining_batch", {}).get("tasks", []))
required = requirements.get("tasks", {})
if len(tasks) != 108 or len(set(tasks)) != 108 or set(tasks) != set(required):
    raise SystemExit("frozen protocol does not define exactly the required 108 tasks")
print("\n".join(tasks))
PY
)
if [[ ${#tasks[@]} -ne 108 ]]; then
  echo "ERROR: failed to load the exact frozen 108-task schedule." >&2
  exit 79
fi

if ! mkdir -p -- "$output_root"; then
  echo "ERROR: cannot create output root: $output_root" >&2
  exit 73
fi
if ! exec 9>"${output_root}/.dsh-108.lock"; then
  echo "ERROR: cannot write to output root: $output_root" >&2
  exit 73
fi
if ! flock -n 9; then
  echo "ERROR: another DSH batch holds ${output_root}/.dsh-108.lock." >&2
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

attempt_is_infra_invalid() {
  python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path

try:
    run = json.loads((Path(sys.argv[1]) / "run.json").read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
invalid = (
    run.get("artifact_gate", {}).get("status") == "passed"
    and run.get("run_validity") == "infra_invalid"
)
raise SystemExit(0 if invalid else 1)
PY
}

completed=0
incomplete=0
for ((index = 0; index < max_tasks; index++)); do
  position=$((index + 1))
  task_id=${tasks[$index]}
  task_root="${runs_root}/${task_id}"
  run_prefix="dsh-108-$(printf '%03d' "$position")-${task_id}"
  a1_id="${run_prefix}-a1"
  a2_id="${run_prefix}-a2"
  a1_dir="${task_root}/${a1_id}"
  a2_dir="${task_root}/${a2_id}"
  mkdir -p -- "$task_root"

  while true; do
    if attempt_complete "$a1_dir" "$task_id" "$a1_id" || \
       attempt_complete "$a2_dir" "$task_id" "$a2_id"; then
      echo "[$position/$max_tasks] SKIP complete: $task_id"
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
      echo "[$position/$max_tasks] preserving a1 and using a2: $task_id" >&2
    else
      echo "[$position/$max_tasks] SKIP incomplete after a1 and a2: $task_id" >&2
      incomplete=$((incomplete + 1))
      break
    fi

    echo "[$position/$max_tasks] START $task_id (a$ordinal)"
    python3 -m astra.runners.toolathlon_dsh.lifecycle \
      --task-id "$task_id" \
      --run-id "$run_id" \
      --output-dir "$run_dir" \
      --toolathlon-source "$source_root" \
      "${replacement_args[@]}"
    status=$?
    if attempt_complete "$run_dir" "$task_id" "$run_id"; then
      echo "[$position/$max_tasks] RECORDED $task_id (process exit $status)"
      completed=$((completed + 1))
      break
    fi
    if [[ $ordinal -eq 1 ]]; then
      if attempt_is_infra_invalid "$run_dir"; then
        echo "[$position/$max_tasks] a1 is infra_invalid; starting a2." >&2
      else
        echo "[$position/$max_tasks] a1 is incomplete; starting a2." >&2
      fi
      continue
    fi
    echo "[$position/$max_tasks] INCOMPLETE $task_id after a2 (process exit $status); continuing." >&2
    incomplete=$((incomplete + 1))
    break
  done
done

echo "DSH batch results: $output_root"
echo "Completed selected tasks: $completed/$max_tasks (full schedule: 108)"
echo "Incomplete selected tasks: $incomplete/$max_tasks"
if (( incomplete > 0 )); then
  exit 1
fi
