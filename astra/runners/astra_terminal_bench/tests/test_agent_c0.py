import asyncio
import contextvars
import hashlib
import json
import shlex
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.trial.trial import Trial

from astra.runners.astra_terminal_bench.agent import AstraTerminalBenchC0Agent
from astra.runners.lifecycle_c0 import LifecycleControllerError


class Result:
    def __init__(self, return_code=0, stdout="", stderr=""):
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


IDENTITY = {
    "pid": 1234,
    "ppid": 1200,
    "pgid": 1234,
    "sid": 1234,
    "start_ticks": 99,
    "exe": "/installed-agent/astra",
    "cgroup": "0::/docker/test\n",
    "supervisor": {
        "pid": 1200,
        "ppid": 1,
        "pgid": 1200,
        "sid": 1200,
        "start_ticks": 90,
        "exe": "/usr/bin/python3",
        "cgroup": "0::/docker/test\n",
    },
}
CLEANUP_REPORT = {
    "schema_version": 1,
    "status": "clean",
    "reason": "normal_exit",
    "fault_action": False,
    "product_terminal_status": "completed",
    "zero_live_proven": True,
    "remaining_pids_count": 0,
    "remaining_pids": [],
}


class FakeEnvironment:
    def __init__(
        self,
        *,
        product_delay=0.03,
        product_return_code=0,
        cleanup_report=None,
        task_workdir="/app",
    ):
        self.product_delay = product_delay
        self.product_return_code = product_return_code
        self.cleanup_report = cleanup_report or CLEANUP_REPORT
        self.task_workdir = task_workdir
        self.commands = []
        self.exec_calls = []
        self.session_id = None
        self.product_started = asyncio.Event()

    async def upload_file(self, source_path, target_path):
        return None

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.commands.append(command)
        self.exec_calls.append(
            {
                "command": command,
                "cwd": cwd,
                "env": env,
                "timeout_sec": timeout_sec,
            }
        )
        if command == "pwd -P":
            return Result(stdout=f"{self.task_workdir}\n")
        if command.startswith("test -d "):
            return Result()
        if "lifecycle-process-probe.py run" in command:
            argv = shlex.split(command)
            if "--session-id" in argv or "--no-resume" not in argv:
                raise AssertionError("the first product turn must create a fresh session")
            self.session_id = str(uuid.uuid4())
            self.retry_overall_deadline_seconds = float(
                argv[argv.index("--overall-deadline-seconds") + 1]
            )
            self.optional_retry_min_remaining_seconds = float(
                argv[
                    argv.index(
                        "--optional-retry-min-remaining-seconds"
                    )
                    + 1
                ]
            )
            self.product_started.set()
            await asyncio.sleep(self.product_delay)
            return Result(return_code=self.product_return_code)
        if "astra-trajectory-export.py discover" in command:
            return Result(stdout=json.dumps({"session_id": self.session_id}))
        if command.startswith("cat ") and command.endswith("product.cleanup.json"):
            return Result(stdout=json.dumps(self.cleanup_report))
        if command.startswith("cat ") and command.endswith("product.identity.json"):
            return Result(stdout=json.dumps(IDENTITY))
        if command.startswith("cat ") and command.endswith("product.stdout"):
            if self.product_return_code != 0:
                return Result(stdout="")
            return Result(
                stdout=json.dumps(
                    {
                        "session_id": self.session_id,
                        "success": True,
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "cache": {"read_tokens": 2},
                    }
                )
            )
        if "lifecycle-process-probe.py inspect" in command:
            return Result(stdout='{"status":"live"}')
        if "lifecycle-predicate-probe.py" in command:
            argv = shlex.split(command)
            predicate_id = argv[argv.index("--predicate") + 1]
            return Result(
                stdout=json.dumps(
                    {
                        "schema_version": 1,
                        "predicate_id": predicate_id,
                        "matched": True,
                        "evidence": {"state": "partial"},
                    }
                )
            )
        return Result()

    async def download_file(self, source_path, target_path):
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source_path.endswith("product.stdout"):
            value = ""
            if self.product_return_code == 0:
                value = json.dumps(
                    {
                        "session_id": self.session_id,
                        "success": True,
                        "prompt_tokens": 10,
                        "completion_tokens": 4,
                        "cache": {"read_tokens": 2},
                    }
                )
            target.write_text(value)
        elif source_path.endswith("product.identity.json"):
            target.write_text(json.dumps(IDENTITY))
        elif source_path.endswith("product.cleanup.json"):
            target.write_text(json.dumps(self.cleanup_report))
        elif source_path.endswith("stream-transport-retry.json"):
            target.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "session_id": self.session_id,
                        "max_retries": 2,
                        "overall_deadline_seconds": (
                            self.retry_overall_deadline_seconds
                        ),
                        "optional_retry_min_remaining_seconds": (
                            self.optional_retry_min_remaining_seconds
                        ),
                        "attempt_count": 1,
                        "retry_count": 0,
                        "attempts": [
                            {
                                "attempt": 1,
                                "input_mode": "initial",
                                "return_code": self.product_return_code,
                                "stream_transport_failure": False,
                            }
                        ],
                        "complete": True,
                        "recovered": False,
                        "exhausted": False,
                        "final_return_code": self.product_return_code,
                    }
                )
            )
        else:
            target.write_text("")

    async def download_dir(self, source_dir, target_dir):
        target = Path(target_dir)
        owner_root = (
            target
            / "local-sessions"
            / "v1"
            / "users"
            / "b64-user"
            / "sessions"
        )
        session_dir = owner_root / self.session_id
        tool_results = session_dir / "tool-results"
        tool_results.mkdir(parents=True, exist_ok=True)
        journal_path = owner_root / f"{self.session_id}.jsonl"
        journal_path.write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "type": "session_start",
                            "session_id": self.session_id,
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn",
                            "session_id": self.session_id,
                        }
                    ),
                    json.dumps(
                        {
                            "type": "session_end",
                            "session_id": self.session_id,
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (session_dir / "step_events.jsonl").write_text(
            '{"event_type":"StepStarted"}\n',
            encoding="utf-8",
        )
        (tool_results / "call-1.txt").write_text("ok", encoding="utf-8")
        server_session_path = target / "server-session.json"
        server_session_path.write_text(
            json.dumps({"session_id": self.session_id}),
            encoding="utf-8",
        )
        server_events_path = target / "server-events.jsonl"
        server_events_path.write_text(
            json.dumps(
                {
                    "event_id": "event-1",
                    "session_id": self.session_id,
                    "event_type": "turn",
                    "content": "complete event content",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        local_files = [
            {
                "path": str(path.relative_to(target)),
                "bytes": path.stat().st_size,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted((target / "local-sessions").rglob("*"))
            if path.is_file()
        ]
        terminal_status = self.cleanup_report["product_terminal_status"]
        (target / "manifest.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "session_id": self.session_id,
                    "product_terminal_status": terminal_status,
                    "failed": terminal_status != "completed",
                    "capture_status": "complete",
                    "server_session_saved": True,
                    "server_session_sha256": hashlib.sha256(
                        server_session_path.read_bytes()
                    ).hexdigest(),
                    "server_events_saved": True,
                    "server_events_sha256": hashlib.sha256(
                        server_events_path.read_bytes()
                    ).hexdigest(),
                    "local_file_count": 3,
                    "local_trace_file_count": 2,
                    "tool_result_file_count": 1,
                    "server_event_count": 1,
                    "local_journal_saved": True,
                    "local_journal_path": str(
                        journal_path.relative_to(target)
                    ),
                    "local_journal_sha256": hashlib.sha256(
                        journal_path.read_bytes()
                    ).hexdigest(),
                    "local_journal_event_count": 3,
                    "local_journal_terminal_event": "session_end",
                    "local_files": local_files,
                    "errors": [],
                }
            ),
            encoding="utf-8",
        )


class BlockingOptionalLogsEnvironment(FakeEnvironment):
    def __init__(self):
        super().__init__()
        self.optional_logs_started = asyncio.Event()

    async def download_dir(self, source_dir, target_dir):
        self.optional_logs_started.set()
        await asyncio.sleep(0.05)
        await super().download_dir(source_dir, target_dir)


class PartialTrajectoryEnvironment(FakeEnvironment):
    async def download_dir(self, source_dir, target_dir):
        await super().download_dir(source_dir, target_dir)
        manifest_path = Path(target_dir) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["capture_status"] = "partial"
        manifest["errors"] = [
            {"source": "server_api", "error": "TimeoutError"}
        ]
        manifest_path.write_text(
            json.dumps(manifest),
            encoding="utf-8",
        )


class MissingTrajectoryEnvironment(FakeEnvironment):
    async def download_dir(self, source_dir, target_dir):
        await super().download_dir(source_dir, target_dir)
        (Path(target_dir) / "manifest.json").unlink()


class AstraC0AgentTests(unittest.IsolatedAsyncioTestCase):
    def test_stream_transport_retry_limit_must_be_non_negative(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                ValueError,
                "stream_transport_retries must be non-negative",
            ):
                AstraTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name="model-id",
                    linux_binary_path="/tmp/not-used",
                    stream_transport_retries=-1,
                    extra_env={
                        "ASTRA_API_URL": "http://host.docker.internal:17001",
                    },
                )

    def test_memory_read_switch_defaults_off_and_accepts_env_override(self):
        with tempfile.TemporaryDirectory() as directory:
            cold_agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
            )
            warm_agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                    "ASTRA_TBENCH_READ_MEMORY": "true",
                },
            )

            self.assertFalse(cold_agent.read_memory)
            self.assertNotIn(
                "ASTRA_MEMORY_READ_ENABLED",
                cold_agent._runtime_env(),
            )
            self.assertEqual(
                cold_agent._runtime_env()["ASTRA_LLM_FALLBACK_TIMEOUT_S"],
                "600",
            )
            self.assertEqual(
                cold_agent._runtime_env()["ASTRA_LLM_TOTAL_BUDGET_S"],
                "27000",
            )
            self.assertTrue(warm_agent.read_memory)
            self.assertNotIn(
                "ASTRA_MEMORY_READ_ENABLED",
                warm_agent._runtime_env(),
            )

    def test_freeze_manifest_sha_must_be_lowercase_sha256(self):
        for value in ("abc", "A" * 64, "g" * 64):
            with self.subTest(value=value):
                with tempfile.TemporaryDirectory() as directory:
                    with self.assertRaisesRegex(
                        ValueError,
                        "must be lowercase SHA-256",
                    ):
                        AstraTerminalBenchC0Agent(
                            logs_dir=Path(directory),
                            model_name="model-id",
                            linux_binary_path="/tmp/not-used",
                            extra_env={
                                "ASTRA_API_URL": (
                                    "http://host.docker.internal:17001"
                                ),
                                "ASTRA_TBENCH_FREEZE_MANIFEST_SHA256": value,
                            },
                        )

    async def test_valid_freeze_manifest_marks_metadata_and_session_formal(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        freeze_sha256 = "a" * 64
        with tempfile.TemporaryDirectory() as directory:
            logs_dir = Path(directory)
            agent = AstraTerminalBenchC0Agent(
                logs_dir=logs_dir,
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                    "ASTRA_TBENCH_FREEZE_MANIFEST_SHA256": freeze_sha256,
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            context = AgentContext()

            await agent.run(instruction, FakeEnvironment(), context)

            self.assertEqual(
                context.metadata["evaluation_status"],
                "formal_frozen_inputs",
            )
            self.assertTrue(context.metadata["formal_score_eligible"])
            self.assertEqual(
                context.metadata["frozen_inputs_manifest_sha256"],
                freeze_sha256,
            )
            session = json.loads(
                (logs_dir / "astra-session.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                session["evaluation_status"],
                "formal_frozen_inputs",
            )
            self.assertTrue(session["formal_score_eligible"])
            self.assertEqual(
                session["frozen_inputs_manifest_sha256"],
                freeze_sha256,
            )
            ledger_rows = [
                json.loads(line)
                for line in (logs_dir / "controller.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            for row in (ledger_rows[0], ledger_rows[-1]):
                self.assertEqual(
                    row["evaluation_status"],
                    "formal_frozen_inputs",
                )
                self.assertTrue(row["formal_score_eligible"])
                self.assertEqual(
                    row["frozen_inputs_manifest_sha256"],
                    freeze_sha256,
                )

    async def test_cold_mode_registers_and_verifies_fresh_user(self):
        class RegistrationEnvironment:
            def __init__(self):
                self.commands = []
                self.username = None

            async def exec(
                self,
                command,
                cwd=None,
                env=None,
                timeout_sec=None,
                user=None,
            ):
                self.commands.append(command)
                argv = shlex.split(command)
                if "register" in argv:
                    self.username = argv[argv.index("--username") + 1]
                    return Result()
                if command.endswith(" whoami"):
                    return Result(
                        stdout=json.dumps(
                            {
                                "user_id": "fresh-user-id",
                                "username": self.username,
                            }
                        )
                    )
                return Result(1)

        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
            )
            environment = RegistrationEnvironment()
            await agent._register_isolated_memory_identity(environment)

            self.assertEqual(agent._memory_user_id, "fresh-user-id")
            self.assertTrue(environment.username.startswith("tbench-"))
            self.assertIn(" register ", f" {environment.commands[0]} ")
            report_text = (
                Path(directory) / "identity-registration.json"
            ).read_text()
            report = json.loads(report_text)
            self.assertEqual(report["status"], "complete")
            self.assertTrue(report["whoami_username_match"])
            self.assertNotIn("Astra-", report_text)

    async def test_cold_mode_retries_register_with_new_identity(self):
        class RegistrationEnvironment:
            def __init__(self):
                self.usernames = []

            async def exec(
                self,
                command,
                cwd=None,
                env=None,
                timeout_sec=None,
                user=None,
            ):
                argv = shlex.split(command)
                if "register" in argv:
                    self.usernames.append(argv[argv.index("--username") + 1])
                    return Result(return_code=1 if len(self.usernames) == 1 else 0)
                if command.endswith(" whoami"):
                    return Result(
                        stdout=json.dumps(
                            {
                                "user_id": "fresh-user-id",
                                "username": self.usernames[-1],
                            }
                        )
                    )
                return Result(1)

        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
            )
            environment = RegistrationEnvironment()
            with mock.patch(
                "astra.runners.astra_terminal_bench.agent.asyncio.sleep",
                new=mock.AsyncMock(),
            ):
                await agent._register_isolated_memory_identity(environment)

            self.assertEqual(len(environment.usernames), 2)
            self.assertNotEqual(environment.usernames[0], environment.usernames[1])
            report = json.loads(
                (Path(directory) / "identity-registration.json").read_text()
            )
            self.assertEqual(report["status"], "complete")
            self.assertEqual(
                [row["stage"] for row in report["attempts"]],
                ["register", "register", "whoami"],
            )

    async def test_cold_mode_whoami_mismatch_fails_closed_without_secrets(self):
        class RegistrationEnvironment:
            async def exec(
                self,
                command,
                cwd=None,
                env=None,
                timeout_sec=None,
                user=None,
            ):
                if " register " in f" {command} ":
                    return Result(stdout="sensitive registration response")
                if command.endswith(" whoami"):
                    return Result(
                        stdout=json.dumps(
                            {
                                "user_id": "wrong-user-id",
                                "username": "wrong-username",
                            }
                        )
                    )
                return Result(1)

        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
            )
            with self.assertRaisesRegex(RuntimeError, "could not verify"):
                await agent._register_isolated_memory_identity(
                    RegistrationEnvironment()
                )

            report_text = (
                Path(directory) / "identity-registration.json"
            ).read_text()
            report = json.loads(report_text)
            self.assertEqual(
                report["failure_stage"],
                "whoami_username_mismatch",
            )
            self.assertNotIn("sensitive registration response", report_text)

    def test_token_is_delayed_until_harbor_final_scrub(self):
        secret = "astra-secret-for-redaction"
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": secret,
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
            )
            phase_env = agent.extra_env
            fake_environment = SimpleNamespace(
                _exec_env_overlays=contextvars.ContextVar(
                    "astra_test_exec_env", default=()
                ),
                _persistent_env={},
            )
            with BaseEnvironment.scoped_exec_env(fake_environment, phase_env):
                agent._arm_harbor_secret_scrub()
                merged = BaseEnvironment._merge_env(fake_environment, None) or {}
                self.assertNotIn("ASTRA_ACCESS_TOKEN", merged)
                self.assertNotIn(secret, merged.values())

            self.assertEqual(agent.extra_env["ASTRA_ACCESS_TOKEN"], secret)
            agent._arm_harbor_secret_scrub()
            self.assertEqual(agent.extra_env["ASTRA_ACCESS_TOKEN"], secret)

            trial_dir = Path(directory) / "trial"
            trial_dir.mkdir()
            output = trial_dir / "output.txt"
            output.write_text(f"before {secret} after", encoding="utf-8")
            fake_trial = SimpleNamespace(
                agent=agent,
                task=SimpleNamespace(
                    config=SimpleNamespace(verifier=SimpleNamespace(env={}))
                ),
                config=SimpleNamespace(verifier=SimpleNamespace(env={})),
                paths=SimpleNamespace(trial_dir=trial_dir),
                logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None),
            )
            Trial._scrub_jobs_dir(fake_trial)
            self.assertEqual(
                output.read_text(encoding="utf-8"),
                "before [REDACTED] after",
            )

    async def test_runs_product_and_noop_controller_without_fault_command(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            environment = FakeEnvironment()
            context = AgentContext()
            await agent.run(instruction, environment, context)

            self.assertTrue(context.metadata["trigger_hit"])
            self.assertTrue(context.metadata["lifecycle_gate_passed"])
            self.assertTrue(context.metadata["product_completion_claim"])
            self.assertFalse(context.metadata["fault_injected"])
            self.assertEqual(context.metadata["approval_policy"], "astra_auto")
            self.assertEqual(
                context.metadata["evaluation_status"],
                "exploratory_unfrozen",
            )
            self.assertFalse(context.metadata["formal_score_eligible"])
            self.assertIsNone(
                context.metadata["frozen_inputs_manifest_sha256"]
            )
            self.assertEqual(context.metadata["llm_fallback_timeout_sec"], 600)
            self.assertEqual(context.metadata["llm_total_budget_sec"], 27000)
            self.assertEqual(context.metadata["task_workdir"], "/app")
            self.assertEqual(context.metadata["stream_transport_retry_limit"], 2)
            self.assertEqual(context.metadata["stream_transport_retry_count"], 0)
            self.assertFalse(context.metadata["stream_transport_recovered"])
            self.assertFalse(
                context.metadata["stream_transport_retry_exhausted"]
            )
            self.assertEqual(context.metadata["astra_session_id"], environment.session_id)
            self.assertEqual(context.metadata["astra_trajectory_status"], "complete")
            self.assertEqual(context.metadata["astra_trajectory_local_file_count"], 3)
            self.assertEqual(
                context.metadata["astra_trajectory_local_trace_file_count"],
                2,
            )
            self.assertEqual(
                context.metadata["astra_trajectory_tool_result_file_count"],
                1,
            )
            self.assertEqual(context.metadata["astra_trajectory_server_event_count"], 1)
            self.assertRegex(
                context.metadata["astra_trajectory_manifest_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertTrue(
                (
                    Path(directory)
                    / "astra-trajectory"
                    / "local-sessions"
                    / "v1"
                    / "users"
                    / "b64-user"
                    / "sessions"
                    / environment.session_id
                    / "step_events.jsonl"
                ).is_file()
            )
            self.assertTrue(
                (
                    Path(directory)
                    / "astra-trajectory"
                    / "local-sessions"
                    / "v1"
                    / "users"
                    / "b64-user"
                    / "sessions"
                    / environment.session_id
                    / "tool-results"
                    / "call-1.txt"
                ).is_file()
            )
            self.assertFalse(
                any(" kill " in f" {command} " for command in environment.commands)
            )
            product_command = next(
                command
                for command in environment.commands
                if "lifecycle-process-probe.py run" in command
            )
            self.assertIn("astra-stream-transport-retry.py", product_command)
            self.assertIn("--max-retries 2", product_command)
            product_exec = next(
                call
                for call in environment.exec_calls
                if "lifecycle-process-probe.py run" in call["command"]
            )
            self.assertEqual(product_exec["cwd"], "/app")
            events = [
                json.loads(line)["event"]
                for line in (Path(directory) / "controller.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertIn("trigger_observed", events)
            self.assertIn("fault_action", events)
            self.assertEqual(events[-1], "controller_completed")
            completed = json.loads(
                (Path(directory) / "controller.jsonl").read_text().splitlines()[-1]
            )
            self.assertEqual(
                completed["astra_trajectory_manifest_sha256"],
                context.metadata["astra_trajectory_manifest_sha256"],
            )
            self.assertGreaterEqual(completed["astra_trajectory_file_count"], 5)
            persisted = next(
                json.loads(line)
                for line in (Path(directory) / "controller.jsonl")
                .read_text()
                .splitlines()
                if json.loads(line)["event"] == "astra_trajectory_persisted"
            )
            self.assertEqual(
                persisted["manifest_sha256"],
                context.metadata["astra_trajectory_manifest_sha256"],
            )
            self.assertEqual(persisted["server_event_count"], 1)
            session = json.loads(
                (Path(directory) / "astra-session.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                session["evaluation_status"],
                "exploratory_unfrozen",
            )
            self.assertFalse(session["formal_score_eligible"])
            self.assertIsNone(session["frozen_inputs_manifest_sha256"])

    async def test_uses_discovered_container_workdir_for_product(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            environment = FakeEnvironment(task_workdir="/workspace")
            context = AgentContext()

            await agent.run(instruction, environment, context)

            product_exec = next(
                call
                for call in environment.exec_calls
                if "lifecycle-process-probe.py run" in call["command"]
            )
            self.assertEqual(product_exec["cwd"], "/workspace")
            self.assertIn("--cwd /workspace", product_exec["command"])
            self.assertEqual(context.metadata["task_workdir"], "/workspace")
            self.assertEqual(
                json.loads(
                    (Path(directory) / "task-workdir.json").read_text()
                )["task_workdir"],
                "/workspace",
            )

    async def test_unknown_task_uses_generic_noop_trigger_with_dataset_timeout(self):
        instruction = "Create /app/answer.txt with the requested result.\n"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task_dir = root / "generic-case"
            logs_dir = root / "trial" / "agent"
            task_dir.mkdir()
            logs_dir.mkdir(parents=True)
            (task_dir / "instruction.md").write_text(
                instruction,
                encoding="utf-8",
            )
            (task_dir / "task.toml").write_text(
                "\n".join(
                    (
                        'schema_version = "1.1"',
                        "[task]",
                        'name = "terminal-bench/generic-case"',
                        "[agent]",
                        "timeout_sec = 900.0",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            (logs_dir.parent / "config.json").write_text(
                json.dumps(
                    {
                        "task": {"path": str(task_dir)},
                        "agent": {
                            "name": (
                                "astra.runners.astra_terminal_bench.agent:"
                                "AstraTerminalBenchC0Agent"
                            )
                        },
                    }
                ),
                encoding="utf-8",
            )
            agent = AstraTerminalBenchC0Agent(
                logs_dir=logs_dir,
                model_name="glm-5.2(thinking:high)",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                    "ASTRA_TBENCH_TEMPERATURE": "0",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=2000,
                product_timeout_multiplier=1.0,
            )
            environment = FakeEnvironment()
            context = AgentContext()

            await agent.run(instruction, environment, context)

            self.assertEqual(context.metadata["condition"], "C0")
            self.assertEqual(context.metadata["task_id"], "generic-case")
            self.assertEqual(
                context.metadata["trigger_registration_status"],
                "generic",
            )
            self.assertEqual(
                context.metadata["trigger_scope"],
                "generic_product_live",
            )
            self.assertEqual(
                context.metadata["trigger_id"],
                "terminal-bench.generic.product-live",
            )
            self.assertEqual(
                context.metadata["configured_product_timeout_sec"],
                900.0,
            )
            self.assertEqual(context.metadata["product_timeout_sec"], 900.0)
            self.assertEqual(context.metadata["product_timeout_multiplier"], 1.0)
            self.assertEqual(context.metadata["thinking_effort"], "high")
            self.assertEqual(context.metadata["temperature_requested"], 0.0)
            self.assertIsNone(context.metadata["temperature_effective"])
            self.assertEqual(
                context.metadata["temperature_policy"],
                "suppressed_by_thinking_protocol",
            )
            self.assertTrue(context.metadata["trigger_hit"])
            self.assertTrue(context.metadata["lifecycle_gate_passed"])
            self.assertFalse(context.metadata["fault_injected"])
            self.assertTrue(
                any(
                    "--predicate terminal-bench.generic.product-live" in command
                    for command in environment.commands
                )
            )
            completed = json.loads(
                (logs_dir / "controller.jsonl").read_text().splitlines()[-1]
            )
            self.assertEqual(
                completed["trigger_registration_status"],
                "generic",
            )

    async def test_optional_log_cancellation_leaves_terminal_ledger(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            environment = BlockingOptionalLogsEnvironment()
            task = asyncio.create_task(
                agent.run(instruction, environment, AgentContext())
            )
            await asyncio.wait_for(
                environment.optional_logs_started.wait(),
                timeout=2,
            )
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            rows = [
                json.loads(line)
                for line in (Path(directory) / "controller.jsonl")
                .read_text()
                .splitlines()
            ]
            self.assertEqual(rows[-1]["event"], "controller_completed")
            self.assertTrue(rows[-1]["adapter_cancelled"])
            persisted = next(
                row
                for row in rows
                if row["event"] == "astra_trajectory_persisted"
            )
            self.assertTrue(persisted["adapter_cancelled"])
            self.assertTrue(persisted["failed"])
            cleanup = next(
                row for row in rows if row["event"] == "product_process_cleanup"
            )
            self.assertTrue(cleanup["zero_live_proven"])
            self.assertLess(cleanup["sequence"], persisted["sequence"])
            self.assertLess(persisted["sequence"], rows[-1]["sequence"])
            session = json.loads(
                (Path(directory) / "astra-session.json").read_text()
            )
            self.assertEqual(session["capture_status"], "complete")
            self.assertTrue(session["adapter_cancelled"])
            self.assertTrue(session["failed"])

    async def test_no_hit_preserves_product_result_for_upstream_verifier(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            context = AgentContext()
            await agent.run(
                instruction,
                FakeEnvironment(product_delay=0),
                context,
            )
            self.assertFalse(context.metadata["trigger_hit"])
            self.assertFalse(context.metadata["lifecycle_gate_passed"])
            self.assertTrue(context.metadata["product_completion_claim"])

    async def test_timeout_keeps_known_session_and_marks_failure(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        timeout_cleanup = {
            **CLEANUP_REPORT,
            "reason": "deadline_expired",
            "product_terminal_status": "timeout",
        }
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            environment = FakeEnvironment(
                product_return_code=124,
                cleanup_report=timeout_cleanup,
            )
            context = AgentContext()
            await agent.run(instruction, environment, context)

            self.assertEqual(context.metadata["astra_session_id"], environment.session_id)
            self.assertEqual(context.metadata["product_terminal_status"], "timeout")
            self.assertFalse(context.metadata["product_completion_claim"])
            session = json.loads(
                (Path(directory) / "astra-session.json").read_text()
            )
            self.assertEqual(session["product_terminal_status"], "timeout")
            self.assertTrue(session["failed"])
            self.assertEqual(session["capture_status"], "complete")

    async def test_missing_cleanup_report_does_not_block_upstream_verifier(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            context = AgentContext()
            with mock.patch(
                "astra.runners.astra_terminal_bench.agent."
                "collect_process_cleanup_report",
                side_effect=LifecycleControllerError(
                    "process cleanup report is unavailable"
                ),
            ):
                await agent.run(
                    instruction,
                    FakeEnvironment(product_return_code=124),
                    context,
                )

            self.assertEqual(
                context.metadata["product_terminal_status"],
                "adapter_infra_error",
            )
            self.assertFalse(
                context.metadata["product_cleanup_zero_live_proven"]
            )
            self.assertEqual(
                context.metadata["product_cleanup_error_type"],
                "LifecycleControllerError",
            )
            self.assertEqual(
                context.metadata["product_cleanup_error"],
                "process cleanup report is unavailable",
            )
            self.assertFalse(context.metadata["product_completion_claim"])
            rows = [
                json.loads(line)
                for line in (Path(directory) / "controller.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            unavailable = next(
                row
                for row in rows
                if row["event"] == "product_process_cleanup_unavailable"
            )
            self.assertEqual(
                unavailable["error_type"],
                "LifecycleControllerError",
            )
            self.assertEqual(rows[-1]["event"], "controller_completed")
            self.assertIsNone(rows[-1]["pending_error_type"])

    async def test_partial_trajectory_is_recorded_without_failing_product(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            context = AgentContext()
            await agent.run(
                instruction,
                PartialTrajectoryEnvironment(),
                context,
            )

            self.assertTrue(
                context.metadata["astra_trajectory_capture_failed"]
            )
            self.assertFalse(
                context.metadata["trajectory_capture_blocking"]
            )
            rows = [
                json.loads(line)
                for line in (Path(directory) / "controller.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(rows[-1]["event"], "controller_completed")
            self.assertTrue(rows[-1]["astra_trajectory_capture_failed"])
            self.assertFalse(rows[-1]["trajectory_capture_blocking"])
            persisted = next(
                row
                for row in rows
                if row["event"] == "astra_trajectory_persisted"
            )
            self.assertTrue(persisted["capture_failed"])
            session = json.loads(
                (Path(directory) / "astra-session.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(session["failed"])

    async def test_missing_trajectory_is_recorded_without_failing_product(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            context = AgentContext()
            await agent.run(
                instruction,
                MissingTrajectoryEnvironment(),
                context,
            )

            self.assertEqual(
                context.metadata["astra_trajectory_status"], "missing"
            )
            self.assertTrue(
                context.metadata["astra_trajectory_capture_failed"]
            )
            self.assertTrue(context.metadata["product_completion_claim"])
            self.assertEqual(context.metadata["product_return_code"], 0)
            session = json.loads(
                (Path(directory) / "astra-session.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertFalse(session["failed"])

    async def test_trajectory_collection_exception_does_not_fail_product(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            agent._collect_c0_logs = mock.AsyncMock(
                side_effect=RuntimeError("collector failed")
            )
            context = AgentContext()
            await agent.run(instruction, FakeEnvironment(), context)

            self.assertEqual(
                context.metadata["astra_trajectory_status"], "missing"
            )
            self.assertTrue(
                context.metadata["astra_trajectory_capture_failed"]
            )
            self.assertTrue(context.metadata["product_completion_claim"])
            status = json.loads(
                (Path(directory) / "trajectory-status.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(
                status["errors"],
                [
                    {
                        "source": "trajectory_collection",
                        "error": "RuntimeError",
                    }
                ],
            )

    async def test_product_cancellation_persists_trajectory_and_terminal_marker(self):
        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text()
        cancelled_cleanup = {
            **CLEANUP_REPORT,
            "reason": "external_cleanup",
            "product_terminal_status": "cancelled",
        }
        with tempfile.TemporaryDirectory() as directory:
            agent = AstraTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="model-id",
                linux_binary_path="/tmp/not-used",
                extra_env={
                    "ASTRA_ACCESS_TOKEN": "test-token",
                    "ASTRA_API_URL": "http://host.docker.internal:17001",
                },
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            environment = FakeEnvironment(
                product_delay=10,
                cleanup_report=cancelled_cleanup,
            )
            context = AgentContext()
            task = asyncio.create_task(agent.run(instruction, environment, context))
            await asyncio.wait_for(environment.product_started.wait(), timeout=2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

            session = json.loads(
                (Path(directory) / "astra-session.json").read_text()
            )
            self.assertEqual(session["astra_session_id"], environment.session_id)
            self.assertEqual(session["product_terminal_status"], "cancelled")
            self.assertTrue(session["failed"])
            self.assertEqual(session["capture_status"], "complete")
            rows = [
                json.loads(line)
                for line in (Path(directory) / "controller.jsonl")
                .read_text()
                .splitlines()
            ]
            terminal = next(
                row for row in rows if row["event"] == "astra_session_terminal"
            )
            self.assertEqual(terminal["product_terminal_status"], "cancelled")
            self.assertTrue(terminal["failed"])
            self.assertEqual(rows[-1]["event"], "controller_completed")
            self.assertTrue(rows[-1]["adapter_cancelled"])
            persisted = next(
                row
                for row in rows
                if row["event"] == "astra_trajectory_persisted"
            )
            self.assertLess(persisted["sequence"], rows[-1]["sequence"])


if __name__ == "__main__":
    unittest.main()
