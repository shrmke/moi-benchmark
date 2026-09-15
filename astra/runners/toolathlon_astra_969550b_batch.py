"""Serial 108-task launcher; no Astra or dataset source modifications."""
from __future__ import annotations

import argparse
import datetime
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import yaml

from toolathlon_verified.contract import read_json_object, utc_now, write_json_atomic

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "work/toolathlon-astra-969550b"
MANIFEST = ROOT / "astra/benchmark/toolathlon-verified/freeze/task-runtime-tiers.json"
COMMIT = "969550b611ceba11653c4b2651079bcb85d1c24e"
KEY_ENV = "TOOLATHLON_DEEPSEEK_ASTRA_API_KEY"


def summarize(state):
    counts = {"total": len(state["tasks"]), "pass": 0, "no_pass": 0, "incomplete": 0, "pending": 0}
    counts["excluded"] = 0
    for task, attempts in state["tasks"].items():
        if task in state.get("excluded_tasks", {}):
            counts["excluded"] += 1
            continue
        if not attempts:
            counts["pending"] += 1
        else:
            last = attempts[-1]
            status = last.get("verify_status")
            if last.get("cleanup_passed") and status in {"pass", "no_pass"}:
                counts[status] += 1
            else:
                counts["incomplete"] += 1
    counts["evaluated"] = counts["pass"] + counts["no_pass"]
    return counts


def save(state, output):
    state["updated_at"] = utc_now()
    state["summary"] = summarize(state)
    write_json_atomic(output / "batch.json", state)
    write_json_atomic(output / "summary.json", state["summary"])


def collect(attempt):
    output = Path(attempt["output_dir"])
    run_path = output / "run.json"
    if run_path.is_file():
        try:
            record = read_json_object(run_path)
            for key in ("terminal_status", "verify_status", "primary_failure_category", "termination_reason", "model_budget"):
                attempt[key] = record.get(key)
        except (ValueError, OSError):
            attempt["result_read_error"] = True
    cleanup = False
    lifecycle = output / "lifecycle-events.jsonl"
    if lifecycle.is_file():
        for line in lifecycle.read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") == "cleanup.end":
                cleanup = event.get("status") == "passed"
    attempt["cleanup_passed"] = cleanup
    return cleanup


def load_key(path):
    configured = os.environ.get(KEY_ENV)
    if configured:
        return configured
    models = yaml.safe_load(path.read_text())
    if not isinstance(models, list):
        raise RuntimeError("Models file must contain a model list")
    matches = [m for m in models if isinstance(m, dict) and m.get("name") == "deepseek-v4-flash"]
    if len(matches) != 1:
        raise RuntimeError("Models file must contain exactly one deepseek-v4-flash entry")
    key = matches[0].get("api_key")
    if isinstance(key, str) and key.startswith("${") and key.endswith("}"):
        key = os.environ.get(key[2:-1])
    if not isinstance(key, str) or not key.strip():
        raise RuntimeError("DeepSeek API key is missing; key values are never printed")
    return key


def has_remaining_runnable(tasks, state, start_index):
    for task in tasks[start_index:]:
        if task in state.get("excluded_tasks", {}):
            continue
        attempts = state["tasks"][task]
        if task in state.get("requeue_tasks", []):
            return True
        if not attempts or not (attempts[-1].get("cleanup_passed") and attempts[-1].get("verify_status") in {"pass", "no_pass"}):
            return True
    return False


def restart_matrixone(state, output):
    event = {"started_at": utc_now(), "after_task_count": state["tasks_since_matrixone_restart"]}
    state.setdefault("matrixone_restart_events", []).append(event)
    state.update(status="restarting_matrixone", current_task=None)
    save(state, output)
    with (output / "matrixone-restarts.log").open("ab") as log:
        restart = subprocess.run(["sudo", "-n", "docker", "restart", "all-in-one-matrixone-1"], stdout=log, stderr=subprocess.STDOUT)
        if restart.returncode:
            event.update(finished_at=utc_now(), status="restart_failed", exit_code=restart.returncode)
            state["status"] = "matrixone_restart_failed"
            save(state, output)
            raise RuntimeError("MatrixOne restart failed; see matrixone-restarts.log")
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            health = subprocess.run(
                ["sudo", "-n", "docker", "inspect", "--format", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", "all-in-one-matrixone-1"],
                stdout=subprocess.PIPE, stderr=log, text=True,
            )
            if health.returncode == 0 and health.stdout.strip() == "healthy":
                event.update(finished_at=utc_now(), status="healthy")
                state["tasks_since_matrixone_restart"] = 0
                state["status"] = "starting"
                save(state, output)
                return
            time.sleep(5)
    event.update(finished_at=utc_now(), status="readiness_timeout")
    state["status"] = "matrixone_restart_failed"
    save(state, output)
    raise RuntimeError("MatrixOne did not become healthy within 300 seconds; see matrixone-restarts.log")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Skip scored and cleaned tasks; retry incomplete tasks once")
    parser.add_argument("--status", action="store_true", help="Read saved progress without loading credentials or starting tasks")
    parser.add_argument("--restore-services", action="store_true", help="Start existing local services without recreating data")
    parser.add_argument("--restart-matrixone-every", type=int, default=0, metavar="N", help="After every N completed tasks, restart MatrixOne and wait until healthy; 0 disables")
    parser.add_argument("--models-file", type=Path, default=ROOT / "external/astra-optimize_0731_05/.models.yaml")
    parser.add_argument("--toolathlon-source", type=Path, default=Path("/home/vagrant/dataset/Toolathlon"))
    args = parser.parse_args()
    if args.restart_matrixone_every < 0:
        parser.error("--restart-matrixone-every must be non-negative")
    output = args.output_dir.expanduser().resolve()
    if args.status:
        state = read_json_object(output / "batch.json")
        print(json.dumps({"status": state["status"], "current_task": state.get("current_task"), "updated_at": state.get("updated_at"), **summarize(state)}, ensure_ascii=False, indent=2))
        return 0

    tasks = list(read_json_object(MANIFEST)["tasks"])
    if len(tasks) != 108:
        raise RuntimeError("Expected the existing 108-task manifest")
    key = load_key(args.models_file.expanduser().resolve())
    WORK.mkdir(parents=True, exist_ok=True)
    # Task preprocess mutates shared local services. Keep this lock in the child
    # too, so killing only the batch parent cannot admit a concurrent batch.
    with (WORK / ".batch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another batch or its active task still holds the shared-service lock") from None
        checkpoint = output / "batch.json"
        if args.resume and checkpoint.is_file():
            state = read_json_object(checkpoint)
            if state.get("astra_commit") != COMMIT or set(state.get("tasks", {})) != set(tasks):
                raise RuntimeError("Existing batch belongs to a different version or task set")
            for attempts in state["tasks"].values():
                if attempts:
                    collect(attempts[-1])
        else:
            if output.exists() and any(output.iterdir()):
                raise RuntimeError("Output is not empty; use --resume or a new output directory")
            output.mkdir(parents=True, exist_ok=True)
            state = {"astra_commit": COMMIT, "model": "deepseek-v4-flash", "thinking": "enabled", "reasoning_effort": "max",
                     "started_at": utc_now(), "task_manifest": str(MANIFEST), "tasks": {task: [] for task in tasks}}
        exclusions_path = output / "excluded-tasks.json"
        if exclusions_path.is_file():
            state["excluded_tasks"] = read_json_object(exclusions_path)
            if set(state["excluded_tasks"]) - set(tasks):
                raise RuntimeError("Exclusion file contains unknown tasks")
        state.setdefault("tasks_since_matrixone_restart", 0)
        state["matrixone_restart_every"] = args.restart_matrixone_every
        state.update(status="starting", current_task=None)
        save(state, output)
        if args.restore_services:
            print("Restoring existing local services...", flush=True)
            with (output / "services.log").open("ab") as log:
                restoration = subprocess.run(["bash", str(ROOT / "astra/runners/restore_toolathlon_services.sh")], stdout=log, stderr=subprocess.STDOUT)
            if restoration.returncode:
                state["status"] = "service_restore_failed"
                save(state, output)
                raise RuntimeError("Service readiness failed; see services.log. No task was started")
        env = os.environ.copy()
        env.update({KEY_ENV: key, "PYTHONPATH": str(ROOT / "astra/runners"), "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost", "PYTHONUNBUFFERED": "1"})
        python = args.toolathlon_source / ".venv/bin/python"
        for ordinal, task in enumerate(tasks, 1):
            attempts = state["tasks"][task]
            if task not in state.get("requeue_tasks", []) and attempts and attempts[-1].get("cleanup_passed") and attempts[-1].get("verify_status") in {"pass", "no_pass"}:
                print(f"[{ordinal}/108] SKIP {task}: {attempts[-1]['verify_status']}", flush=True)
                continue
            if attempts and not attempts[-1].get("cleanup_passed"):
                # No scoring or automatic retry may hide an uncertain teardown.
                prior = Path(attempts[-1]["output_dir"]) / "lifecycle-events.jsonl"
                if prior.exists():
                    state.update(status="cleanup_requires_attention", current_task=task)
                    save(state, output)
                    raise RuntimeError(f"Previous cleanup for {task} was not confirmed; inspect its lifecycle log before resuming")
            if task in state.get("excluded_tasks", {}):
                print(f"[{ordinal}/108] EXCLUDE {task}: {state['excluded_tasks'][task]}", flush=True)
                continue
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            run_id = f"astra969-{task}-{stamp}"
            slot = output / task / f"attempt-{len(attempts) + 1}-{stamp}"
            slot.parent.mkdir(parents=True, exist_ok=True)
            launch_log = slot.with_suffix(".launch.log")
            attempt = {"run_id": run_id, "output_dir": str(slot), "launch_log": str(launch_log), "started_at": utc_now()}
            attempts.append(attempt)
            if task in state.get("requeue_tasks", []):
                state["requeue_tasks"].remove(task)
            state.update(status="running", current_task=task)
            save(state, output)
            command = [str(python), "-m", "toolathlon_astra_969550b", "--system", "astra", "--task-id", task,
                       "--experiment-id", "toolathlon-astra-969550b-108", "--run-id", run_id, "--output-dir", str(slot),
                       "--toolathlon-source", str(args.toolathlon_source), "--docker-via-sudo"]
            print(f"[{ordinal}/108] START {task} -> {slot}", flush=True)
            fd = os.open(launch_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as log:
                child = subprocess.Popen(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                         start_new_session=True, pass_fds=(lock.fileno(),))
                attempt["pid"] = child.pid
                save(state, output)
                try:
                    attempt["runner_exit_code"] = child.wait()
                except KeyboardInterrupt:
                    child.send_signal(signal.SIGINT)
                    try:
                        attempt["runner_exit_code"] = child.wait(timeout=180)
                    except subprocess.TimeoutExpired:
                        print(f"Task PID {child.pid} is still shutting down; no further task will start", flush=True)
                    collect(attempt)
                    state["status"] = "interrupted"
                    save(state, output)
                    return 130
            attempt["finished_at"] = utc_now()
            clean = collect(attempt)
            state["current_task"] = None
            save(state, output)
            print(f"[{ordinal}/108] END {task}: agent={attempt.get('terminal_status', 'unavailable')} evaluator={attempt.get('verify_status', 'unavailable')} cleanup={clean}", flush=True)
            if not clean:
                state["status"] = "cleanup_requires_attention"
                save(state, output)
                raise RuntimeError("Cleanup was not confirmed; stopping before the next shared-service task")
            if args.restart_matrixone_every:
                state["tasks_since_matrixone_restart"] += 1
                save(state, output)
                if state["tasks_since_matrixone_restart"] >= args.restart_matrixone_every and has_remaining_runnable(tasks, state, ordinal):
                    print(f"MatrixOne boundary reached after {state['tasks_since_matrixone_restart']} tasks; restarting before the next task", flush=True)
                    restart_matrixone(state, output)
                    print("MatrixOne is healthy; continuing with the next task", flush=True)
        state.update(status="finished", current_task=None, finished_at=utc_now())
        save(state, output)
        print(json.dumps(state["summary"], ensure_ascii=False, indent=2), flush=True)
        return 0 if state["summary"]["evaluated"] + state["summary"].get("excluded", 0) == 108 else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError, ValueError) as exc:
        print(f"Batch stopped: {exc}", file=sys.stderr)
        raise SystemExit(2)
