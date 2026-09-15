"""Single-task Astra 969550b deployment; reuses Toolathlon lifecycle, not old agent freeze."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pymysql

from toolathlon_verified import lifecycle as base
from toolathlon_verified.adapter_common import write_astra_credentials
from toolathlon_verified.astra_969550b_adapter import AstraRuntime, _run_admin, run_astra, write_astra_runtime_mcp_binding
from toolathlon_verified.bundle import write_public_bundle
from toolathlon_verified.contract import JsonlEventWriter, ModelFreeze, RunSpec, read_json_object, sha256_file, utc_now, write_json_atomic
from toolathlon_verified.mcp_client import capture_tool_manifest
from toolathlon_verified.model_proxy import ModelProxyConfig, ModelProxyServer as PooledModelProxyServer, _UpstreamConnectionPool, wait_for_model_requests_to_settle
from toolathlon_verified.orchestrator import _adapter_failure, _failure_category, _redact_if_needed, _run_evaluator, _write_official_trajectory
from toolathlon_verified.permissions import PermissionPolicy
from toolathlon_verified.product_identity import _request_json
from toolathlon_verified.resources import ResourceSampler
from toolathlon_verified.trajectory import normalize_product_events

COMMIT = "969550b611ceba11653c4b2651079bcb85d1c24e"
IMAGE = "astra-969550b:local"
KEY_ENV = "TOOLATHLON_DEEPSEEK_ASTRA_API_KEY"
ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "work/astra-optimize-0731-05-linux-amd64/target/release/astra"


def docker(*args, timeout=120, check=True):
    return subprocess.run(["sudo", "-n", "docker", *map(str, args)], capture_output=True, timeout=timeout, check=check)


class FreshConnectionPool(_UpstreamConnectionPool):
    def release(self, connection, *, reusable):
        super().release(connection, reusable=False)


class ModelProxyServer(PooledModelProxyServer):
    def _handler_type(self):
        pool = self.upstream_pool
        self.upstream_pool = FreshConnectionPool(pool._factory, maximum=pool._maximum)
        pool.close()
        return super()._handler_type()


class TaskServer:
    """Host networking only; no host home, dataset, evaluator or Docker socket mounts."""
    def __init__(self, workspace, output, proxy_url):
        self.workspace, self.output, self.proxy_url = workspace, output, proxy_url
        suffix = secrets.token_hex(6)
        self.name = "toolathlon-astra-969-" + suffix
        self.database = "astra_toolathlon_shared_969550b"
        self.model_name = "deepseek-v4-flash-" + suffix
        self.port = base._free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.runtime = AstraRuntime(executable=CLI, server_executable=Path("/usr/local/bin/astra-server"), api_url=self.url,
                                    admin_token_env="ASTRA_ADMIN_ACCESS_TOKEN", server_mode="task_scoped_host_network_container", configure_model=False)
        self.started = False
        self.cleanup_needed = False
        self.shared_cleanup_error = None

    def start(self):
        config_path = Path(os.environ.get("TOOLATHLON_ASTRA_SERVER_CONFIG", str(ROOT / "work/toolathlon-astra-969550b/server-runtime.private.json")))
        if config_path.stat().st_mode & 0o077:
            raise RuntimeError("Astra deployment config must be private (chmod 600)")
        env = read_json_object(config_path)
        required = ("MATRIXONE_USER", "MATRIXONE_PASSWORD", "MEMORIA_MASTER_KEY",
                    "ASTRA_JWT_SECRET", "ASTRA_TOKEN_ENCRYPTION_KEY", "ASTRA_BRIDGE_SECRET")
        if any(not isinstance(env.get(key), str) or not env[key] for key in required):
            raise RuntimeError("Astra deployment config is missing required credentials")
        # Keep database credentials inside deployment; never pass the provider key to Astra.
        env = {k: v for k, v in env.items() if k.startswith(("ASTRA_", "MATRIXONE_")) or k in {"MEMORIA_BASE_URL", "MEMORIA_MASTER_KEY", "RUST_LOG", "PATH"}}
        env.update(ASTRA_API_HOST="127.0.0.1", ASTRA_API_PORT=str(self.port), MATRIXONE_HOST="127.0.0.1", MATRIXONE_PORT="6001",
                   ASTRA_DATABASE=self.database, ASTRA_DATABASE_PREFIX="", ASTRA_AUTO_CREATE_DATABASE="true",
                   MEMORIA_BASE_URL="http://127.0.0.1:18100", HOME="/tmp", NO_PROXY="*", no_proxy="*")
        connection = pymysql.connect(host="127.0.0.1", port=6001, user=env["MATRIXONE_USER"], password=env["MATRIXONE_PASSWORD"], connect_timeout=10, autocommit=True)
        try:
            with connection.cursor() as cursor:
                cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{self.database}`")
        finally:
            connection.close()
        from toolathlon_verified.astra_shared_lifecycle import assert_clean
        assert_clean(self.output)
        self.cleanup_needed = True
        # Reuse a dedicated catalog; task containers, identities and workspaces remain separate.
        with tempfile.TemporaryDirectory(prefix="toolathlon-astra-deploy-") as private:
            private = Path(private)
            envfile = private / "server.env"
            envfile.touch(mode=0o600)
            envfile.write_text("".join(f"{k}={v}\n" for k, v in env.items()))
            result = docker("run", "-d", "--name", self.name, "--network", "host", "--read-only", "--cap-drop", "ALL",
                            "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}",
                            "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777", "--env-file", envfile,
                            "--mount", f"type=bind,src={self.workspace},dst=/workspace/dumps/workspace",
                            "--workdir", "/workspace/dumps/workspace", IMAGE, check=False)
            if result.returncode:
                raise RuntimeError("Astra container creation failed; no provider request was made")
            self.started = True
            until = time.monotonic() + 180
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", self.port), timeout=2):
                        break
                except OSError:
                    state = json.loads(docker("inspect", self.name).stdout)[0]["State"]
                    if not state["Running"]:
                        raise RuntimeError("Astra exited during startup; see astra-server.log")
                    if time.monotonic() >= until:
                        raise RuntimeError("Astra startup deadline exceeded; see astra-server.log")
                    time.sleep(2)
            username = "ta_admin_" + secrets.token_hex(6)
            _, identity = _request_json("POST", self.url + "/auth/register", body={"username": username,
                "email": username + "@toolathlon.invalid", "password": secrets.token_urlsafe(32), "display_name": None}, timeout=60)
            # Grant this task identity admin in the dedicated Toolathlon catalog.
            connection = pymysql.connect(host="127.0.0.1", port=6001, user=env["MATRIXONE_USER"], password=env["MATRIXONE_PASSWORD"],
                                         database=self.database, connect_timeout=10, autocommit=True)
            try:
                with connection.cursor() as cursor:
                    cursor.execute("INSERT IGNORE INTO auth_user_roles (user_id, role_id) SELECT %s, role_id FROM auth_roles WHERE role_name = %s",
                                   (identity["user_id"], "astra_admin"))
            finally:
                connection.close()
            credentials = private / "credentials"
            credentials.mkdir(mode=0o700)
            write_json_atomic(credentials / "credentials.json", {"current_profile": "default", "profiles": {"default": {"access_token": identity["access_token"], "account_id": identity["user_id"], "username": username}}})
            for args in (["model", "add", self.model_name, "openai", "--api-key", "toolathlon-local-proxy", "--context-window", "1000000", "--base-url", "http://127.0.0.1:9/v1"],
                         ["model", "update", self.model_name, "--base-url", self.proxy_url, "--active", "true", "--quirks", '{"wire_model_name":"deepseek-v4-flash","fallback_chain":[]}']):
                result = _run_admin(self.runtime, credentials, args, home=private)
                if result.returncode:
                    raise RuntimeError("Astra model provisioning failed: " + str(result.stderr or result.stdout).replace(identity["access_token"], "[REDACTED]")[:2000])
            _, model_record = _request_json("GET", self.url + "/models/" + self.model_name, access_token=identity["access_token"])
            os.environ["TOOLATHLON_ASTRA_OFFERING_ID"] = model_record["model_id"]
        return self.runtime

    def close(self):
        if self.started:
            docker("stop", "-t", "10", self.name, check=False)
            log = docker("logs", self.name, check=False)
            (self.output / "astra-server.log").write_bytes(log.stdout + log.stderr)
            removal = docker("rm", self.name, check=False)
            if removal.returncode:
                raise RuntimeError("Astra container cleanup failed; evaluator remains isolated")
            self.started = False
        if self.cleanup_needed:
            from toolathlon_verified.astra_shared_lifecycle import cleanup
            try:
                cleanup(self.output, IMAGE)
                self.cleanup_needed = False
            except Exception as exc:
                self.shared_cleanup_error = str(exc)



def run_slot(config_path, *, before_evaluator, lifecycle_writer, resource_sampler=None, on_product_pid=None, **_unused):
    raw = read_json_object(config_path)
    spec = RunSpec.from_dict(raw["run"])
    model = ModelFreeze.load(spec.model_freeze_path)
    started = utc_now()
    output = spec.output_dir
    events = JsonlEventWriter(output / "adapter-events.jsonl", run_id=spec.run_id, system_id="astra")
    public_path = output / "task-bundle.public.json"
    public = write_public_bundle(spec.bundle_file, public_path, expected_task_id=spec.task_id, workspace=spec.workspace)
    PermissionPolicy.load(spec.permission_policy_path, expected_gateway_url=spec.gateway_url, expected_workspace=spec.workspace)
    model_state = output / "model-proxy-state.json"
    proxy_config = ModelProxyConfig(upstream_base_url=model.provider_base_url, upstream_api_key=os.environ[KEY_ENV],
        effective_model=model.request_model_id, temperature=model.temperature, thinking=model.thinking, reasoning_effort=model.reasoning_effort,
        max_requests=spec.max_model_requests, run_id=spec.run_id, system_id="astra", events_path=output / "model-usage.jsonl", state_path=model_state)
    observed = None
    server = None
    adapter_start = time.monotonic()
    try:
        lifecycle_writer.append("tools_list.start")
        observed = capture_tool_manifest(task_id=spec.task_id, gateway_url=spec.gateway_url,
                                        destination=output / "tool-schema-observed.json", timeout_s=120)
        if observed.get("run_qualification") != "go":
            raise RuntimeError("Gateway tool name qualification failed")
        binding = write_astra_runtime_mcp_binding(output, observed, gateway_url=spec.gateway_url)
        lifecycle_writer.append("tools_list.end", tool_count=observed["tool_count"])
        with ModelProxyServer(proxy_config) as proxy:
            server = TaskServer(spec.workspace, output, proxy.url)
            runtime = server.start()
            write_json_atomic(output / "resolved-config.json", {"system_id": "astra", "astra_commit": COMMIT, "image": IMAGE,
                "task_id": spec.task_id, "run_id": spec.run_id, "benchmark_scope": "single_task_new_astra_version_not_historical_frozen_cohort",
                "model": {"id": model.request_model_id, "thinking": model.thinking, "reasoning_effort": model.reasoning_effort,
                          "base_url": model.provider_base_url}, "deadline_s": spec.deadline_s, "max_model_requests": spec.max_model_requests,
                "api_url": server.url, "database": server.database, "network_mode": "host",
                "workspace_mount": {"source": str(spec.workspace), "destination": "/workspace/dumps/workspace"},
                "dataset_mounted_to_astra": False, "evaluator_mounted_to_astra": False})
            lifecycle_writer.append("adapter.start")
            outcome = run_astra(runtime=runtime, public_bundle=public, gateway_url=spec.gateway_url, workspace=spec.workspace,
                output_dir=output, proxy_url=proxy.url, deadline_seconds=spec.deadline_s, budget_exceeded=proxy.budget.exceeded.is_set,
                model_request_snapshot=proxy.budget.snapshot, experiment_id=spec.experiment_id, task_id=spec.task_id, run_id=spec.run_id,
                attempt_ordinal=1, runtime_mcp_binding_path=binding["path"], task_mcp_tool_names=binding["tool_names"],
                on_product_pid=on_product_pid, on_agent_start=lambda: events.append("agent.execution_start", deadline_seconds=spec.deadline_s))
            outcome.metadata["post_terminal_model_drain"] = wait_for_model_requests_to_settle(proxy.budget.snapshot, context="Astra post-terminal")
    except Exception as exc:
        outcome = _adapter_failure(exc, time.monotonic() - adapter_start)
        events.append("adapter.error", error_type=type(exc).__name__, error=str(exc))
    finally:
        # Stop the process before private evaluator artifacts are restored.
        if server is not None:
            server.close()
            if server.shared_cleanup_error:
                outcome.metadata["shared_database_cleanup_error"] = server.shared_cleanup_error
                events.append("adapter.cleanup_error", error=server.shared_cleanup_error)
    summary = normalize_product_events(outcome.native_events, run_id=spec.run_id, system_id="astra", trajectory_path=output / "trajectory.jsonl",
                                       tool_calls_path=output / "tool-calls.jsonl", observed_tool_manifest=observed)
    _write_official_trajectory(spec, outcome=outcome, trajectory_summary=summary)
    lifecycle_writer.append("adapter.end", terminal_status=outcome.terminal_status)
    before_evaluator(outcome)
    lifecycle_writer.append("evaluator.start")
    evaluator = _run_evaluator(raw["evaluator"], spec=spec, public_bundle_path=public_path, evaluator_timeout_seconds=1800, agent_exit_code=(128 - outcome.product_exit_code if outcome.product_exit_code is not None and outcome.product_exit_code < 0 else outcome.product_exit_code if outcome.product_exit_code is not None else 1))
    lifecycle_writer.append("evaluator.end", **evaluator)
    failure = _failure_category(outcome, evaluator)
    record = {"schema_version": 1, "run_id": spec.run_id, "experiment_id": spec.experiment_id, "task_id": spec.task_id, "system_id": "astra",
        "astra_commit": COMMIT, "started_at": started, "finished_at": utc_now(), "terminal_status": outcome.terminal_status,
        "product_exit_code": outcome.product_exit_code, "termination_reason": outcome.termination_reason, "deadline_s": spec.deadline_s,
        "verify_status": evaluator["verify_status"], "reward": evaluator.get("reward"), "evaluator": evaluator,
        "primary_failure_category": failure, "adapter": outcome.metadata, "trajectory": summary,
        "model_budget": read_json_object(model_state).get("budget") if model_state.exists() else None}
    write_json_atomic(output / "run.json", record)
    write_json_atomic(output / "failure-evidence.json", {"run_id": spec.run_id, "task_id": spec.task_id, "primary_failure_category": failure,
                                                      "terminal_status": outcome.terminal_status, "evaluator": evaluator})
    paths = [p for p in output.rglob("*") if p.is_file() and p.name != "product-identity.private.json"]
    _redact_if_needed(paths, [os.environ[KEY_ENV], *outcome.sensitive_values])
    return 0 if evaluator["verify_status"] in {"pass", "no_pass"} and outcome.terminal_status in {"completed", "timeout", "max_steps"} else 2


class Lifecycle(base.SingleTaskLifecycle):
    def _preprocess(self):
        if "woocommerce" in self.task_reset_contract["required_mcp_servers"]:
            self._docker("exec", self.container_name, "mkdir", "-p", "/tmp/toolathlon-prepare-policy", timeout=30)
            self._docker("cp", str(ROOT / "astra/runners/toolathlon_prepare_policy/sitecustomize.py"),
                         f"{self.container_name}:/tmp/toolathlon-prepare-policy/sitecustomize.py", timeout=30)
        return super()._preprocess()

    def _cleanup(self):
        super()._cleanup()
        marker = self.output / "shared-db-cleanup.json"
        if marker.exists() and read_json_object(marker).get("status") != "passed":
            self.lifecycle.append("cleanup.end", status="failed", reason="shared_database_cleanup_failed")
            raise base.LifecycleError("Shared Astra database cleanup failed; original run result retained")

    def docker_command(self, *args):
        command = super().docker_command(*args)
        if args and args[0] == "run" and "rail_12306" in self.task_reset_contract["required_mcp_servers"]:
            preinstalled = ROOT / "work/toolathlon-astra-969550b/rail-preinstalled-0.3.9"
            if not (preinstalled / "node_modules/.bin/12306-mcp").exists():
                raise base.LifecycleError("Preinstalled 12306-mcp@0.3.9 is missing")
            position = command.index("run") + 1
            command[position:position] = [
                "--mount", f"type=bind,src={preinstalled},dst=/opt/rail,readonly",
                "--env", "PATH=/opt/rail/bin:/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            ]
        if "scripts.decoupled.container_preprocess" in args:
            log_path = self.output / "preprocess.log"
            log_path.touch(mode=0o600, exist_ok=True)
            os.chmod(log_path, 0o600)
            command.append("--debug")
            if "woocommerce" in self.task_reset_contract["required_mcp_servers"]:
                position = command.index("exec") + 1
                command[position:position] = [
                    "--env", "PYTHONPATH=/tmp/toolathlon-prepare-policy:/workspace",
                    "--env", "TOOLATHLON_LOCAL_WOO_PREPARE_POLICY=1",
                ]
        return command

    def _docker(self, *args, **kwargs):
        result = super()._docker(*args, **kwargs)
        if args and args[0] == "cp" and self.private_dir is not None:
            destination = Path(str(args[-1]))
            if destination.parent == self.private_dir and destination.exists():
                subprocess.run(["sudo", "-n", "chown", "-hR", f"{os.getuid()}:{os.getgid()}", str(destination)], check=True, capture_output=True)
        return result

    def _guard_command(self, action, *args):
        result = super()._guard_command(action, *args)
        if action == "stash" and self.private_dir is not None:
            subprocess.run(["sudo", "-n", "chown", "-hR", f"{os.getuid()}:{os.getgid()}", str(self.private_dir / "artifact-stash")], check=True, capture_output=True)
        return result

    def preflight(self):
        if self.args.system != "astra":
            raise base.LifecycleError("This entry point runs Astra tasks from the existing Toolathlon requirements manifest")
        if not os.environ.get(KEY_ENV):
            raise base.LifecycleError(f"Set {KEY_ENV}; never put it in command arguments")
        self.task_reset_contract = base.load_task_reset_contract(task_id=self.args.task_id, task_source=self.task_source,
                                                               requirements_path=self.freeze / "task-requirements.json")
        if not self.host_python.is_file() or not self.runtime_overlay.is_file() or not CLI.is_file():
            raise base.LifecycleError("Required runner runtime is unavailable")
        self._docker("image", "inspect", base.TASK_IMAGE, timeout=60)
        docker("image", "inspect", IMAGE)
        manifest = read_json_object(self.freeze / "credential-manifest.json")
        mutable = []
        for item in manifest["toolathlon_application_credentials"]["files"]:
            relative = Path(item["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise base.LifecycleError("Unsafe application credential path")
            path = self.source / relative
            if not path.is_file() or path.is_symlink():
                raise base.LifecycleError(f"Missing application credential: {relative}")
            if base.is_runtime_mutable_credential_record(item):
                mutable.append(str(relative))
            if not path.resolve().is_relative_to(self.source.resolve()):
                raise base.LifecycleError(f"Application credential escapes source root: {relative}")
        self.mutable_credential_paths = sorted(mutable)

    def _write_slot_config(self, gateway_port):
        path = super()._write_slot_config(gateway_port)
        config = read_json_object(path)
        config["evaluator"]["command"].append("--evaluate_regardless_of_agent_status")
        write_json_atomic(path, config)
        return path

    def execute(self):
        if self.output.exists() and any(self.output.iterdir()):
            raise base.LifecycleError("Output directory must be absent or empty")
        self.output.mkdir(parents=True, exist_ok=True)
        self.lifecycle = JsonlEventWriter(self.output / "lifecycle-events.jsonl", run_id=self.args.run_id, system_id="astra")
        self.private_dir = Path(tempfile.mkdtemp(prefix="toolathlon-969-"))
        os.chmod(self.private_dir, 0o700)
        sampler = ResourceSampler(self.output / "resource-usage.jsonl", run_id=self.args.run_id, system_id="astra",
            product_pid=lambda: self.product_pid[0], container_id=lambda: self.container_id[0], container_pid=lambda: self.container_pid[0])
        sampler.start()
        try:
            self.preflight()
            self._reset()
            self._start_container()
            self._copy_tree()
            self._preprocess()
            self._stash_private_artifacts()
            port = base._free_port()
            self._start_gateway(port)
            return run_slot(self._write_slot_config(port), before_evaluator=self._restore_for_evaluator, lifecycle_writer=self.lifecycle,
                            resource_sampler=sampler, on_product_pid=lambda pid: self.product_pid.__setitem__(0, pid))
        finally:
            try:
                self._cleanup()
            finally:
                sampler.close()
                shutil.rmtree(self.private_dir, ignore_errors=True)
                self.private_dir = None


def main():
    args = base.parse_args()
    return Lifecycle(args).execute()


if __name__ == "__main__":
    raise SystemExit(main())
