"""Two bounded real-agent lifecycle tasks; not official Toolathlon scores."""
from pathlib import Path
import datetime
import fcntl
import json
import os
import secrets
import subprocess
import time
import yaml
import toolathlon_astra_969550b as m
from toolathlon_astra_969550b_batch import load_key
from toolathlon_verified.product_identity import provision_astra_identity
from toolathlon_verified.astra_shared_lifecycle import connect, counts


def main():
    os.umask(0o077)
    root = Path(__file__).resolve().parents[2]
    work = root / "work/toolathlon-astra-969550b"
    lock = (work / ".batch.lock").open("a+")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    output = work / ("two-task-isolation-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    output.mkdir()
    print("OUTPUT", output, flush=True)
    model_path = root / "external/astra-optimize_0731_05/.models.yaml"
    row = next(x for x in yaml.safe_load(model_path.read_text()) if x.get("name") == "deepseek-v4-flash")
    key = load_key(model_path)
    results = []
    for index, (prompt, expected) in enumerate([
        ("Compute 19 + 23. Reply exactly TASK_A=42. Do not call tools.", "TASK_A=42"),
        ("Compute 7 * 9. Reply exactly TASK_B=63. Do not call tools.", "TASK_B=63"),
    ], 1):
        slot = output / str(index)
        slot.mkdir()
        workspace = slot / "workspace"
        workspace.mkdir()
        if index == 1:
            (workspace / "first-task-only.txt").write_text(secrets.token_hex(16))
        connection, _ = connect()
        before = counts(connection)
        connection.close()
        assert not any(before.values()), before
        proxy = m.ModelProxyServer(m.ModelProxyConfig(upstream_base_url=row["base_url"], upstream_api_key=key, effective_model="deepseek-v4-flash", temperature=0, thinking="enabled", reasoning_effort="max", max_requests=100, run_id=f"isolation-{index}", system_id="astra", events_path=slot / "model-usage.jsonl", state_path=slot / "model-proxy-state.json"))
        server = None
        result = {"task": index, "before": before, "expected": expected}
        try:
            with proxy:
                server = m.TaskServer(workspace, slot, proxy.url)
                t = time.monotonic()
                server.start()
                result["startup_seconds"] = round(time.monotonic() - t, 3)
                probe = subprocess.run(["sudo", "-n", "docker", "exec", server.name, "test", "-e", "/workspace/dumps/workspace/first-task-only.txt"], capture_output=True, timeout=10)
                result["first_task_file_visible"] = probe.returncode == 0
                assert result["first_task_file_visible"] == (index == 1)
                identity = provision_astra_identity(api_url=server.url, output_dir=slot, experiment_id="shared-db-isolation", task_id=f"minimal-{index}", run_id=f"isolation-{index}", attempt_ordinal=1)
                env = os.environ.copy()
                for name in list(env):
                    if name.endswith("API_KEY") or name in {"ASTRA_ADMIN_ACCESS_TOKEN", "ASTRA_ACCESS_TOKEN"}:
                        env.pop(name, None)
                home = slot / "home"
                home.mkdir()
                env.update(HOME=str(home), ASTRA_API_URL=server.url, ASTRA_ACCESS_TOKEN=identity.access_token, NO_PROXY="*", no_proxy="*")
                command = [str(m.CLI), "chat", "--no-resume", "--json", "--no-color", "--permission-mode", "deny", "--model", server.model_name, "--message", prompt]
                t = time.monotonic()
                child = subprocess.run(command, env=env, cwd=home, capture_output=True, text=True, timeout=180)
                (slot / "agent.stdout.log").write_text(child.stdout)
                (slot / "agent.stderr.log").write_text(child.stderr)
                result.update(agent_exit_code=child.returncode, agent_seconds=round(time.monotonic()-t, 3), response_contains_expected=expected in child.stdout)
                connection, _ = connect()
                with connection.cursor() as cur:
                    cur.execute("SELECT session_id,user_id,status FROM agent_sessions")
                    result["sessions_during_task"] = cur.fetchall()
                    cur.execute("SELECT run_id,status FROM agent_runs")
                    result["runs_during_task"] = cur.fetchall()
                connection.close()
                result["budget"] = proxy.budget.snapshot()
        except Exception as exc:
            result["error"] = type(exc).__name__ + ": " + str(exc)
        finally:
            if server:
                server.close()
                result["cleanup_error"] = server.shared_cleanup_error
        connection, _ = connect()
        result["after"] = counts(connection)
        connection.close()
        result["passed"] = not result.get("error") and result.get("agent_exit_code") == 0 and result.get("response_contains_expected") and not result.get("cleanup_error") and not any(result["after"].values())
        results.append(result)
        (output / "results.json").write_text(json.dumps(results, indent=2, default=str))
        print("RESULT", json.dumps(result, default=str), flush=True)
        if not result["passed"]:
            return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
