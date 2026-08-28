#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
workspace_root="$(cd -- "${script_dir}/../../.." && pwd)"
data_root="${MOI_BENCH_DATA_ROOT:-${workspace_root}}"
tasks_root="${data_root}/work/terminal-bench-2-1/tasks"
config="${workspace_root}/astra/runners/dsh_terminal_bench/c0-terminalbench-89-glm52.yaml"
shared_queue_root="${workspace_root}/astra/runners/pi_terminal_bench/prebuilt"
queue_builder="${shared_queue_root}/resource_queue.py"
schedule="${shared_queue_root}/schedule.py"
state_dir="${data_root}/work/dsh-c0-terminalbench-89-glm52-state"
canonical_queue="${state_dir}/resource.queue.tsv"
queue="${canonical_queue}"
jobs_dir="${data_root}/work/dsh-c0-terminalbench-89-glm52-jobs"
check_only=false
max_tasks=""
retry_queue=""

usage() {
  cat <<'EOF'
Run or resume the DSH cohort from the 89-task Terminal-Bench 2.1 snapshot.

Usage: dsh-terminal-bench-all-c0.sh [--check] [--max-tasks N]
                                      [--retry-queue FILE]

The queue excludes tune-mjcf, matching Pi, so 88 tasks are scheduled.
Completed tasks with valid verifier evidence are skipped automatically.
The shared Pi scheduler uses three 2GB memory tokens: 8GB tasks run alone,
4GB tasks may overlap one 2GB task, and up to three 2GB tasks may overlap.
With --retry-queue, listed tasks run even if they already have valid results.
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --check) check_only=true; shift ;;
    --max-tasks) max_tasks="$2"; shift 2 ;;
    --retry-queue) retry_queue="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[[ -z "${max_tasks}" || "${max_tasks}" =~ ^[1-9][0-9]*$ ]] || {
  echo "--max-tasks must be positive" >&2
  exit 2
}
[[ -d "${tasks_root}" ]] || {
  echo "missing dataset tasks: ${tasks_root}" >&2
  exit 2
}

if [[ -n "${HARBOR_BIN:-}" ]]; then
  harbor_bin="${HARBOR_BIN}"
elif command -v harbor >/dev/null 2>&1; then
  harbor_bin="$(command -v harbor)"
else
  harbor_bin="${HOME}/.local/share/uv/tools/harbor/bin/harbor"
fi

if [[ ! -x "${harbor_bin}" ]]; then
  echo "Harbor executable not found; set HARBOR_BIN to its absolute path" >&2
  exit 1
fi

python_bin="${PYTHON_BIN:-}"
if [[ -z "${python_bin}" ]]; then
  if [[ -x "$(dirname -- "${harbor_bin}")/python" ]]; then
    python_bin="$(dirname -- "${harbor_bin}")/python"
  else
    python_bin="python3"
  fi
fi

cd -- "${workspace_root}"
export PYTHONPATH="${workspace_root}${PYTHONPATH:+:${PYTHONPATH}}"

mkdir -p "${state_dir}"
"${python_bin}" "${queue_builder}" \
  --tasks-root "${tasks_root}" \
  --output "${canonical_queue}"

if [[ -n "${retry_queue}" ]]; then
  [[ -f "${retry_queue}" ]] || {
    echo "missing retry queue: ${retry_queue}" >&2
    exit 2
  }
  while IFS= read -r retry_row; do
    [[ -z "${retry_row}" ]] && continue
    grep -Fqx -- "${retry_row}" "${canonical_queue}" || {
      echo "retry queue row is not in the canonical queue: ${retry_row}" >&2
      exit 2
    }
  done < "${retry_queue}"
  queue="${retry_queue}"
fi

cohort_args=(
  --expected-agent
  astra.runners.dsh_terminal_bench.agent:DshTerminalBenchC0Agent
  --expected-model
  zai/glm-5.2
  --expected-version
  0.1.0rc6
  --cohort-kwarg
  'profile="terminalbench-glm52"'
)
schedule_args=(
  "${schedule}"
  --queue "${queue}"
  --jobs-dir "${jobs_dir}"
  --generated-root "${tasks_root}"
  --config "${config}"
  --workspace-root "${workspace_root}"
  --harbor-bin "${harbor_bin}"
  --print-pending
  "${cohort_args[@]}"
)
[[ -z "${max_tasks}" ]] || schedule_args+=(--max-tasks "${max_tasks}")
[[ -z "${retry_queue}" ]] || schedule_args+=(--rerun-completed)

pending_queue="${state_dir}/pending.queue.tsv"
"${python_bin}" "${schedule_args[@]}" > "${pending_queue}"
pending_count="$(sed '/^[[:space:]]*$/d' "${pending_queue}" | wc -l | tr -d '[:space:]')"
echo "Queue: ${canonical_queue}"
echo "Pending: ${pending_count}/88"
echo "Policy: shared Pi scheduler; 3 memory tokens, 8GB isolated"

if [[ "${check_only}" == "true" ]]; then
  exit 0
fi
if [[ "${pending_count}" == "0" ]]; then
  echo "All cohort tasks already have terminal verifier results."
  exit 0
fi
if [[ -z "${ZAI_API_KEY:-}" ]]; then
  echo "ZAI_API_KEY must be exported before starting the run" >&2
  exit 1
fi

run_args=(
  "${schedule}"
  --queue "${pending_queue}"
  --jobs-dir "${jobs_dir}"
  --generated-root "${tasks_root}"
  --config "${config}"
  --workspace-root "${workspace_root}"
  --harbor-bin "${harbor_bin}"
  "${cohort_args[@]}"
)
[[ -z "${retry_queue}" ]] || run_args+=(--rerun-completed)

set +e
"${python_bin}" "${run_args[@]}"
run_status="$?"
set -e

refresh_args=(
  "${schedule}"
  --queue "${canonical_queue}"
  --jobs-dir "${jobs_dir}"
  --generated-root "${tasks_root}"
  --config "${config}"
  --workspace-root "${workspace_root}"
  --harbor-bin "${harbor_bin}"
  --print-pending
  "${cohort_args[@]}"
)
"${python_bin}" "${refresh_args[@]}" > "${pending_queue}"
remaining_count="$(sed '/^[[:space:]]*$/d' "${pending_queue}" | wc -l | tr -d '[:space:]')"
echo "Remaining: ${remaining_count}/88"
exit "${run_status}"
