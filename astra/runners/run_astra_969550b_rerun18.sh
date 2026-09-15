#!/usr/bin/env bash
set -euo pipefail
set +x
cd /home/vagrant/moi-benchmark
export PYTHONPATH="$PWD/astra/runners${PYTHONPATH:+:$PYTHONPATH}"
export ASTRA_RERUN_OUTPUT="$PWD/work/toolathlon-astra-969550b/rerun-authorized-18-20260914"
# Prepare a separate selection checkpoint; never alter the original 108-task batch.
/home/vagrant/dataset/Toolathlon/.venv/bin/python - <<'PY'
import fcntl, json, os
from pathlib import Path
from toolathlon_astra_969550b_batch import WORK, MANIFEST, COMMIT
from toolathlon_verified.contract import write_json_atomic, utc_now
selected = ["fillout-online-forms","inter-final-performance-analysis","interview-report","investment-decision-analysis","k8s-pr-preview-testing","k8s-redis-helm-upgrade","k8s-safety-audit","language-school","latex-prompt-box","live-transactions","llm-training-dataset","logical-datasets-collection","quantitative-financial-analysis","vlm-history-completer","wandb-best-score","woocommerce-new-product","woocommerce-stock-alert","woocommerce-update-cover"]
output = Path(os.environ["ASTRA_RERUN_OUTPUT"])
all_tasks = list(json.loads(MANIFEST.read_text())["tasks"])
with (WORK / ".batch.lock").open("a+") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    checkpoint = output / "batch.json"
    if checkpoint.exists():
        state = json.loads(checkpoint.read_text())
        if state.get("selected_rerun_tasks") != selected:
            raise RuntimeError("Existing rerun checkpoint has a different task selection")
    else:
        output.mkdir(parents=True, exist_ok=True)
        exclusions = {task: "Not in user-authorized 18-task full-rerun selection" for task in all_tasks if task not in selected}
        state = {
            "astra_commit": COMMIT, "model": "deepseek-v4-flash",
            "thinking": "enabled", "reasoning_effort": "max",
            "started_at": utc_now(), "task_manifest": str(MANIFEST),
            "tasks": {task: [] for task in all_tasks},
            "status": "prepared", "current_task": None,
            "selected_rerun_tasks": selected, "excluded_tasks": exclusions,
            "source_batch": str(WORK / "batch-108-resumable"),
            "note": "Independent full reruns; original attempts remain unchanged. 90 exclusions here mean unselected, not benchmark account exclusions.",
        }
        write_json_atomic(output / "excluded-tasks.json", exclusions)
        write_json_atomic(checkpoint, state)
PY
exec bash astra/runners/run_astra_969550b_108.sh --output-dir "$ASTRA_RERUN_OUTPUT" --resume --restart-matrixone-every 5 "$@"
