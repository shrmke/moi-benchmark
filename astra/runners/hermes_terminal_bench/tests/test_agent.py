from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext
from harbor.trial.trial import Trial

from astra.runners.hermes_terminal_bench.agent import (
    DEEPSEEK_MODEL_NAME,
    FROZEN_HERMES_VERSION,
    FROZEN_MAX_TURNS,
    FROZEN_MODEL_NAME,
    FROZEN_PLAYWRIGHT_RELEASE,
    HermesTerminalBenchC0Agent,
    REMOTE_HERMES_PYTHON,
    _ENSURE_PYTHON3_COMMAND,
)
from astra.runners.hermes_terminal_bench.prebuilt.configure_temperature import (
    INSERT_AFTER,
    apply_temperature,
    normalize_temperature,
)


class HermesC0AgentTests(unittest.TestCase):
    def _agent(self, directory: str) -> HermesTerminalBenchC0Agent:
        return HermesTerminalBenchC0Agent(
            logs_dir=Path(directory),
            model_name=FROZEN_MODEL_NAME,
            version=FROZEN_HERMES_VERSION,
            extra_env={"GLM_API_KEY": "offline-placeholder"},
        )

    def test_provider_key_is_removed_from_general_exec_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)

        self.assertNotIn("GLM_API_KEY", agent._extra_env)
        self.assertNotIn("GLM_API_KEY", agent._product_env())

    def test_glm52_managed_profile_uses_high_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)
            managed = yaml.safe_load(
                agent._managed_config_path().read_text(encoding="utf-8")
            )

        self.assertEqual(managed["agent"]["reasoning_effort"], "high")
        self.assertEqual(agent._reasoning_effort, "high")

    def test_deepseek_flash_managed_profile_uses_max_effort(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=DEEPSEEK_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"DEEPSEEK_API_KEY": "offline-placeholder"},
            )
            managed = yaml.safe_load(
                agent._managed_config_path().read_text(encoding="utf-8")
            )

        self.assertEqual(managed["model"]["provider"], "deepseek")
        self.assertEqual(managed["model"]["default"], "deepseek-v4-flash")
        self.assertEqual(managed["agent"]["reasoning_effort"], "max")
        self.assertEqual(agent._reasoning_effort, "max")
        self.assertIsNone(agent._temperature)
        self.assertNotIn("DEEPSEEK_API_KEY", agent._extra_env)

    def test_product_timeout_multiplier_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self._agent(directory).product_timeout_multiplier, 2.0)
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                product_timeout_multiplier=1.0,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
            )
            self.assertEqual(agent.product_timeout_multiplier, 1.0)

            with self.assertRaisesRegex(ValueError, "must be positive"):
                HermesTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name=FROZEN_MODEL_NAME,
                    version=FROZEN_HERMES_VERSION,
                    product_timeout_multiplier=0,
                    extra_env={"GLM_API_KEY": "offline-placeholder"},
                )

    def test_provider_key_is_delayed_until_harbor_final_scrub(self) -> None:
        for key_name in ("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"):
            with self.subTest(key_name=key_name), tempfile.TemporaryDirectory() as directory:
                secret = f"{key_name.lower()}-secret-for-redaction"
                agent = HermesTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name=FROZEN_MODEL_NAME,
                    version=FROZEN_HERMES_VERSION,
                    extra_env={key_name: secret},
                )
                phase_env = agent.extra_env
                fake_environment = SimpleNamespace(
                    _exec_env_overlays=contextvars.ContextVar(
                        f"hermes_test_exec_env_{key_name}", default=()
                    ),
                    _persistent_env={},
                )
                with BaseEnvironment.scoped_exec_env(
                    fake_environment, phase_env
                ):
                    agent._arm_harbor_secret_scrub()
                    merged = (
                        BaseEnvironment._merge_env(fake_environment, None) or {}
                    )
                    self.assertNotIn(key_name, merged)
                    self.assertNotIn(secret, merged.values())

                self.assertEqual(agent.extra_env[key_name], secret)
                agent._arm_harbor_secret_scrub()
                self.assertEqual(agent.extra_env[key_name], secret)

                trial_dir = Path(directory) / "trial"
                trial_dir.mkdir()
                output = trial_dir / "output.txt"
                output.write_text(
                    f"before {secret} after",
                    encoding="utf-8",
                )
                fake_trial = SimpleNamespace(
                    agent=agent,
                    task=SimpleNamespace(
                        config=SimpleNamespace(
                            verifier=SimpleNamespace(env={})
                        )
                    ),
                    config=SimpleNamespace(
                        verifier=SimpleNamespace(env={})
                    ),
                    paths=SimpleNamespace(trial_dir=trial_dir),
                    logger=SimpleNamespace(
                        debug=lambda *_args, **_kwargs: None
                    ),
                )
                Trial._scrub_jobs_dir(fake_trial)
                self.assertEqual(
                    output.read_text(encoding="utf-8"),
                    "before [REDACTED] after",
                )

    def test_managed_config_freezes_security_and_runtime_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)
            managed_bytes = agent._managed_config_path().read_bytes()
            managed = yaml.safe_load(managed_bytes)
            managed_env_bytes = agent._managed_env_path().read_bytes()
            managed_env = dict(
                line.split("=", 1)
                for line in managed_env_bytes.decode().splitlines()
                if line
            )
            user = yaml.safe_load(
                agent._build_c0_config_yaml(agent.max_turns)
            )

        self.assertEqual(managed["model"]["provider"], "zai")
        self.assertEqual(managed["model"]["default"], "glm-5.2")
        self.assertEqual(managed["approvals"]["mode"], "smart")
        self.assertEqual(managed["command_allowlist"], [])
        self.assertFalse(managed["quick_commands"])
        self.assertFalse(managed["hooks"])
        self.assertFalse(managed["hooks_auto_accept"])
        self.assertFalse(managed["mcp_servers"])
        self.assertFalse(managed["mcp"]["auto_reload_on_config_change"])
        self.assertFalse(managed["memory"]["memory_enabled"])
        self.assertFalse(managed["checkpoints"]["enabled"])
        self.assertEqual(managed["agent"]["gateway_timeout"], 24000)
        self.assertEqual(
            managed_env,
            {
                "HERMES_HOME": "/tmp/hermes",
                "HERMES_MANAGED_DIR": "/etc/hermes",
                "HERMES_YOLO_MODE": "0",
                "HERMES_ACCEPT_HOOKS": "0",
                "HERMES_EXEC_ASK": "1",
            },
        )
        self.assertEqual(user, {"agent": {"max_turns": FROZEN_MAX_TURNS}})
        self.assertEqual(
            hashlib.sha256(managed_bytes).hexdigest(),
            agent._managed_config_sha256(),
        )
        self.assertEqual(
            hashlib.sha256(managed_env_bytes).hexdigest(),
            agent._managed_env_sha256(),
        )
        self.assertEqual(
            hashlib.sha256(agent._policy_guard_path().read_bytes()).hexdigest(),
            agent._policy_guard_sha256(),
        )

    def test_managed_config_mount_must_be_read_only(self) -> None:
        mountinfo = (
            "36 25 0:32 /source /etc/hermes ro,relatime "
            "- virtiofs source rw\n"
        )
        self.assertTrue(
            HermesTerminalBenchC0Agent._mount_is_read_only(
                mountinfo, "/etc/hermes"
            )
        )
        self.assertFalse(
            HermesTerminalBenchC0Agent._mount_is_read_only(
                mountinfo.replace(" ro,", " rw,"),
                "/etc/hermes",
            )
        )

    def test_product_process_pins_managed_scope_and_disables_yolo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = self._agent(directory)._product_env()

        self.assertEqual(env["HERMES_HOME"], "/tmp/hermes")
        self.assertEqual(env["HERMES_MANAGED_DIR"], "/etc/hermes")
        self.assertEqual(env["HERMES_YOLO_MODE"], "0")
        self.assertEqual(env["HERMES_ACCEPT_HOOKS"], "0")

    def test_c0_manifest_mounts_managed_directory(self) -> None:
        config_path = Path(
            "astra/runners/hermes_terminal_bench/c0-four-cases.yaml"
        )
        mounts = {
            mount["target"]: mount
            for mount in yaml.safe_load(
                config_path.read_text(encoding="utf-8")
            )["environment"]["mounts"]
        }

        self.assertTrue(mounts["/etc/hermes"]["read_only"])
        self.assertEqual(
            mounts["/etc/hermes"]["source"],
            "${MOI_BENCH_ROOT}/astra/runners/hermes_terminal_bench/managed",
        )
        self.assertNotIn("/logs/agent/.env", mounts)

    def test_prebuilt_manifest_requires_preinstalled_agent(self) -> None:
        config_path = Path(
            "astra/runners/hermes_terminal_bench/"
            "c0-four-cases-prebuilt.yaml"
        )
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))

        self.assertTrue(config["agents"][0]["kwargs"]["preinstalled"])
        self.assertEqual(
            config["datasets"][0]["path"],
            "work/terminal-bench-2-1-hermes-prebuilt/tasks",
        )
        self.assertTrue(
            config["environment"]["mounts"][0]["read_only"]
        )
        with tempfile.TemporaryDirectory() as directory:
            marker = self._agent(directory)._expected_prebuilt_marker()
        self.assertEqual(
            marker["playwright_release"],
            FROZEN_PLAYWRIGHT_RELEASE,
        )

    def test_full_prebuilt_manifest_and_queue_cover_snapshot(self) -> None:
        runner = Path("astra/runners/hermes_terminal_bench")
        config = yaml.safe_load(
            (runner / "c0-all-prebuilt.yaml").read_text(encoding="utf-8")
        )
        full_wrapper = Path(
            "astra/runners/scripts/hermes-terminal-bench-all-c0.sh"
        ).read_text(encoding="utf-8")
        queue = [
            line
            for line in (
                runner / "prebuilt" / "c0-all.queue.txt"
            ).read_text(encoding="utf-8").splitlines()
            if line
        ]
        tasks = sorted(
            path.parent.name
            for path in Path(
                "work/terminal-bench-2-1/tasks"
            ).glob("*/task.toml")
        )

        self.assertEqual(queue, tasks)
        self.assertEqual(len(queue), 89)
        self.assertEqual(
            config["agents"][0]["kwargs"]["turn_timeout_sec"],
            24000,
        )
        self.assertEqual(
            config["agents"][0]["kwargs"]["trigger_timeout_sec"],
            24000,
        )
        self.assertEqual(config["n_concurrent_trials"], 1)
        self.assertTrue(config["agents"][0]["kwargs"]["preinstalled"])
        self.assertIn(
            ".moi-hermes-c0-cohort.sha256",
            full_wrapper,
        )
        self.assertIn("--cohort-fingerprint", full_wrapper)
        self.assertIn("refusing to mix result generations", full_wrapper)
        self.assertIn(
            "astra/runners/astra_smoke/probe.py",
            full_wrapper,
        )
        self.assertIn(
            "astra/runners/astra_smoke/core.py",
            full_wrapper,
        )
        self.assertIn(
            "astra/runners/lifecycle_c0/__init__.py",
            full_wrapper,
        )
        self.assertGreaterEqual(
            full_wrapper.count("verify_current_cohort"),
            3,
        )

    def test_prebuilt_builder_uses_one_shared_runtime(self) -> None:
        root = Path(
            "astra/runners/hermes_terminal_bench/prebuilt"
        )
        script = (root / "build-images.sh").read_text(encoding="utf-8")
        runtime_dockerfile = (
            root / "Dockerfile"
        ).read_text(encoding="utf-8")
        task_dockerfile = (
            root / "Dockerfile.task"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'RUNTIME_IMAGE="${HERMES_PREBUILT_RUNTIME_IMAGE:-',
            script,
        )
        self.assertEqual(
            script.count('--file "${SCRIPT_DIR}/Dockerfile"'),
            1,
        )
        self.assertIn(
            '--file "${SCRIPT_DIR}/Dockerfile.task"',
            script,
        )
        self.assertIn("verify_task_image()", script)
        self.assertNotIn("--no-cache", script)
        self.assertIn("docker image rm --force", script)
        self.assertIn("at least one task or --queue-file is required", script)
        self.assertIn('[[ "${driver}" == "docker" ]]', script)
        self.assertIn('BUILD_PROXY="${HERMES_TBENCH_BUILD_PROXY:-}"', script)
        self.assertEqual(
            script.count('"${BUILD_NETWORK_ARGS[@]}"'),
            2,
        )
        self.assertEqual(
            script.count('"${BUILD_PROXY_ARGS[@]}"'),
            2,
        )
        self.assertIn('--build-arg "HTTPS_PROXY=${BUILD_PROXY}"', script)
        self.assertIn(
            "refusing to overwrite unrecognized runtime image",
            script,
        )
        self.assertIn(
            'io.moi.hermes-tbench.kind="ephemeral-task"',
            task_dockerfile,
        )
        self.assertNotIn("install-deps --dry-run chromium", script)
        self.assertIn(
            "FROM ${HERMES_RUNTIME_IMAGE} AS hermes_runtime",
            task_dockerfile,
        )
        self.assertIn(
            "FROM scratch",
            runtime_dockerfile,
        )
        self.assertNotIn("/usr/local/bin/hermes-acp", runtime_dockerfile)
        self.assertNotIn("/usr/local/bin/hermes-acp", task_dockerfile)
        self.assertIn("UV_MANAGED_PYTHON=1", runtime_dockerfile)
        self.assertIn(
            "/usr/local/share/uv/python/*",
            runtime_dockerfile,
        )
        self.assertIn(
            "/usr/local/share/uv/python/*",
            task_dockerfile,
        )
        self.assertIn(
            "/usr/local/share/uv/python/*",
            script,
        )
        self.assertIn(
            "ln -sfn /root/.hermes/node/bin/node /usr/local/bin/node",
            task_dockerfile,
        )
        self.assertNotIn(
            'ln -sfn "${python_real}" /usr/local/bin/python3',
            task_dockerfile,
        )
        self.assertIn(
            "if ! command -v python3 >/dev/null 2>&1",
            task_dockerfile,
        )
        self.assertIn(
            'ln -s "${python_real}" /usr/local/bin/python3',
            task_dockerfile,
        )
        self.assertIn(
            'readlink -f /usr/local/bin/node',
            script,
        )
        self.assertGreaterEqual(
            task_dockerfile.count("COPY --link --from=hermes_runtime"),
            5,
        )
        self.assertEqual(runtime_dockerfile.count("Acquire::Retries=5"), 2)
        self.assertNotIn("apt-get", task_dockerfile)
        self.assertNotIn("ms-playwright", task_dockerfile)
        self.assertIn('TASK_LAYOUT="terminal-only-v2"', script)
        self.assertIn(
            'io.moi.hermes-tbench.layout="${HERMES_TASK_LAYOUT}"',
            task_dockerfile,
        )
        self.assertIn("Reusing verified task image", script)

    def test_prebuilt_temperature_is_frozen_and_audited(self) -> None:
        root = Path("astra/runners/hermes_terminal_bench/prebuilt")
        runtime_dockerfile = (root / "Dockerfile").read_text(
            encoding="utf-8"
        )
        task_dockerfile = (root / "Dockerfile.task").read_text(
            encoding="utf-8"
        )
        script = (root / "build-images.sh").read_text(encoding="utf-8")

        self.assertIn("ARG HERMES_TEMPERATURE=0.0", runtime_dockerfile)
        self.assertIn(
            "--temperature \"${HERMES_TEMPERATURE}\"",
            runtime_dockerfile,
        )
        self.assertIn(
            '"temperature_source":"provider_profile.fixed_temperature"',
            runtime_dockerfile,
        )
        self.assertIn(
            'io.moi.hermes-tbench.temperature="${HERMES_TEMPERATURE}"',
            runtime_dockerfile,
        )
        self.assertIn(
            "/opt/moi/hermes-temperature.json",
            task_dockerfile,
        )
        self.assertIn(
            'TEMPERATURE="0.0"',
            script,
        )
        self.assertNotIn("HERMES_TEMPERATURE:-", script)
        self.assertGreaterEqual(
            script.count(
                '--build-arg "HERMES_TEMPERATURE=${TEMPERATURE}"'
            ),
            2,
        )
        self.assertIn(
            "io.moi.hermes-tbench.temperature-configurator-sha256",
            script,
        )

    def test_temperature_configurator_accepts_zero_and_fails_closed(
        self,
    ) -> None:
        self.assertEqual(normalize_temperature("0"), "0.0")
        self.assertEqual(normalize_temperature("0.20"), "0.2")
        for invalid in ("-0.1", "1.1", "0.001", "nan"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                normalize_temperature(invalid)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            target = (
                repo / "plugins" / "model-providers" / "zai" / "__init__.py"
            )
            target.parent.mkdir(parents=True)
            source = (
                "from providers import register_provider\n"
                "zai = ZaiProfile(\n"
                f"{INSERT_AFTER}"
                ")\n"
            )
            target.write_text(source, encoding="utf-8")
            source_sha256 = hashlib.sha256(source.encode()).hexdigest()
            with mock.patch(
                "astra.runners.hermes_terminal_bench.prebuilt."
                "configure_temperature.EXPECTED_ZAI_SOURCE_SHA256",
                source_sha256,
            ):
                audit = apply_temperature(repo, root / "audit", "0")

            self.assertIn(
                "    fixed_temperature=0.0,\n",
                target.read_text(encoding="utf-8"),
            )
            self.assertEqual(audit["temperature"], 0.0)
            self.assertEqual(
                (root / "audit" / "hermes-temperature").read_text(
                    encoding="utf-8"
                ),
                "0.0\n",
            )
            self.assertEqual(
                hashlib.sha256(
                    (root / "audit" / "hermes-temperature.patch").read_bytes()
                ).hexdigest(),
                audit["patch_sha256"],
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "source digest",
            ):
                apply_temperature(repo, root / "audit-2", "0")
            target.write_text(source, encoding="utf-8")
            with self.assertRaisesRegex(
                ValueError,
                "frozen at 0.0",
            ):
                apply_temperature(repo, root / "audit-3", "0.2")

    def test_prebuilt_queue_requires_explicit_tasks(self) -> None:
        script = Path(
            "astra/runners/hermes_terminal_bench/prebuilt/build-images.sh"
        )
        result = subprocess.run(
            ["/bin/bash", str(script)],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "at least one task or --queue-file is required",
            result.stderr,
        )

    def test_prebuilt_queue_file_is_ordered_and_deduplicated(self) -> None:
        script = Path(
            "astra/runners/hermes_terminal_bench/prebuilt/build-images.sh"
        )
        with tempfile.TemporaryDirectory() as directory:
            queue = Path(directory) / "queue.txt"
            queue.write_text(
                (
                    "# queued C0 tasks\n"
                    "overfull-hbox\n"
                    "modernize-scientific-stack  # inline comment\n"
                    "overfull-hbox\n"
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(script),
                    "--print-queue",
                    "build-pmars",
                    "--queue-file",
                    str(queue),
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.splitlines(),
            [
                "build-pmars",
                "overfull-hbox",
                "modernize-scientific-stack",
            ],
        )

    def test_prebuilt_queue_accepts_generic_c0_task_before_docker(
        self,
    ) -> None:
        script = Path(
            "astra/runners/hermes_terminal_bench/prebuilt/build-images.sh"
        )
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            task.mkdir()
            (task / "task.toml").write_text(
                'docker_image = "example/regex-log:latest"\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(script),
                    "--tasks-root",
                    directory,
                    "--print-queue",
                    "regex-log",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["regex-log"])

    def test_prebuilt_queue_requires_full_config_for_generic_task(
        self,
    ) -> None:
        script = Path(
            "astra/runners/hermes_terminal_bench/prebuilt/build-images.sh"
        )
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / "regex-log"
            task.mkdir()
            (task / "task.toml").write_text(
                'docker_image = "example/regex-log:latest"\n',
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(script),
                    "--tasks-root",
                    directory,
                    "regex-log",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "the default four-case config cannot run regex-log",
            result.stderr,
        )
        self.assertIn("c0-all-prebuilt.yaml", result.stderr)

    def test_full_result_summary_creates_resume_queue(self) -> None:
        script = Path(
            "astra/runners/hermes_terminal_bench/prebuilt/"
            "summarize_results.py"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = root / "queue.txt"
            queue.write_text(
                "task-a\ntask-b\ntask-c\n",
                encoding="utf-8",
            )
            result_path = (
                root
                / "jobs"
                / "2026-01-01__00-00-00"
                / "task-a__trial"
                / "result.json"
            )
            result_path.parent.mkdir(parents=True)
            result_path.write_text(
                json.dumps(
                    {
                        "task_name": "terminal-bench/task-a",
                        "finished_at": "2026-01-01T00:01:00Z",
                        "config": {
                            "install_only": False,
                            "agent": {
                                "name": (
                                    "astra.runners.hermes_terminal_bench."
                                    "agent:HermesTerminalBenchC0Agent"
                                ),
                                "model_name": "zai/glm-5.2",
                                "kwargs": {
                                    "version": "v2026.7.20",
                                    "preinstalled": True,
                                },
                            },
                        },
                        "agent_result": {
                            "n_input_tokens": 10,
                            "n_output_tokens": 2,
                            "metadata": {
                                "trajectory_capture_status": "saved",
                                "product_cleanup_zero_live_proven": True,
                                "hermes_prebuilt_marker_verified": True,
                                "hermes_prebuilt_marker_sha256": "a" * 64,
                                "trigger_scope": "generic_product_live",
                            },
                        },
                        "verifier_result": {
                            "rewards": {"reward": 1.0}
                        },
                        "exception_info": None,
                    }
                ),
                encoding="utf-8",
            )
            exception_path = (
                root
                / "jobs"
                / "2026-01-01__00-02-00"
                / "task-b__trial"
                / "result.json"
            )
            exception_path.parent.mkdir(parents=True)
            exception_path.write_text(
                json.dumps(
                    {
                        "task_name": "terminal-bench/task-b",
                        "finished_at": "2026-01-01T00:03:00Z",
                        "config": {
                            "install_only": False,
                            "agent": {
                                "name": (
                                    "astra.runners.hermes_terminal_bench."
                                    "agent:HermesTerminalBenchC0Agent"
                                ),
                                "model_name": "zai/glm-5.2",
                                "kwargs": {
                                    "version": "v2026.7.20",
                                    "preinstalled": True,
                                },
                            },
                        },
                        "agent_result": {
                            "metadata": {
                                "hermes_prebuilt_marker_verified": True,
                                "hermes_prebuilt_marker_sha256": "a" * 64,
                                "trigger_scope": "task_specific_progress",
                            }
                        },
                        "verifier_result": {
                            "rewards": {"reward": 1.0}
                        },
                        "exception_info": {
                            "exception_type": "RuntimeError"
                        },
                    }
                ),
                encoding="utf-8",
            )
            output = root / "state"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--jobs-dir",
                    str(root / "jobs"),
                    "--queue-file",
                    str(queue),
                    "--output-dir",
                    str(output),
                    "--dataset-commit",
                    "test-commit",
                    "--cohort-fingerprint",
                    "b" * 64,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            summary = json.loads(
                (output / "summary.json").read_text(encoding="utf-8")
            )
            pending = (output / "pending.queue.txt").read_text(
                encoding="utf-8"
            )
            retried = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--jobs-dir",
                    str(root / "jobs"),
                    "--queue-file",
                    str(queue),
                    "--output-dir",
                    str(output),
                    "--dataset-commit",
                    "test-commit",
                    "--cohort-fingerprint",
                    "b" * 64,
                    "--retry-audit-failures",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            retry_pending = (output / "pending.queue.txt").read_text(
                encoding="utf-8"
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(retried.returncode, 0, retried.stderr)
        self.assertEqual(summary["recorded_tasks"], 2)
        self.assertEqual(summary["pending_tasks"], 1)
        self.assertEqual(summary["mean_reward"], 0.5)
        self.assertEqual(summary["dataset_commit"], "test-commit")
        self.assertEqual(summary["cohort_fingerprint"], "b" * 64)
        self.assertEqual(
            summary["aggregation_mode"],
            "latest_finished_attempt_per_task",
        )
        self.assertEqual(summary["selected_attempts_per_task"], 1)
        self.assertEqual(summary["valid_c0_tasks"], 0)
        self.assertEqual(
            summary["verifier_status_counts"]["exception"],
            1,
        )
        self.assertEqual(
            summary["c0_audit_status_counts"]["infra_error"],
            2,
        )
        self.assertEqual(
            summary["trigger_scope_counts"]["generic_product_live"],
            1,
        )
        self.assertEqual(summary["results"][0]["scored_reward"], 1.0)
        self.assertEqual(summary["results"][1]["scored_reward"], 0.0)
        self.assertIn(
            "controller.jsonl",
            summary["results"][0]["c0_audit_failures"],
        )
        self.assertEqual(pending, "task-c\n")
        self.assertEqual(retry_pending, "task-a\ntask-b\ntask-c\n")

    def test_prebuilt_queue_rejects_generated_tasks_as_source(self) -> None:
        script = Path(
            "astra/runners/hermes_terminal_bench/prebuilt/build-images.sh"
        )
        task_name = "modernize-scientific-stack"
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory) / task_name
            task.mkdir()
            (task / "task.toml").write_text(
                (
                    "docker_image = "
                    f'"moi/hermes-tbench-{task_name}:v2026.7.20"\n'
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    "/bin/bash",
                    str(script),
                    "--tasks-root",
                    directory,
                    "--print-queue",
                    task_name,
                ],
                check=False,
                capture_output=True,
                text=True,
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn(
            "tasks-root points to a generated Hermes task image",
            result.stderr,
        )

    def test_prepare_tasks_copies_only_explicit_selection(self) -> None:
        script = Path(
            "astra/runners/hermes_terminal_bench/prebuilt/prepare_tasks.py"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            destination = root / "generated"
            for task_name in ("task-one", "task-two"):
                task = source / task_name
                task.mkdir(parents=True)
                (task / "task.toml").write_text(
                    'docker_image   =   "example/original:latest"  \n',
                    encoding="utf-8",
                )
                (task / "marker.txt").write_text(
                    task_name,
                    encoding="utf-8",
                )
            result = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--source",
                    str(source),
                    "--destination",
                    str(destination),
                    "task-two",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((destination / "task-one").exists())
            self.assertEqual(
                (destination / "task-two" / "marker.txt").read_text(
                    encoding="utf-8"
                ),
                "task-two",
            )
            self.assertIn(
                'docker_image = "moi/hermes-tbench-task-two:v2026.7.20"',
                (destination / "task-two" / "task.toml").read_text(
                    encoding="utf-8"
                ),
            )

    def test_prebuilt_queue_cleans_failed_entry_before_next_build(
        self,
    ) -> None:
        script = Path(
            "astra/runners/hermes_terminal_bench/prebuilt/build-images.sh"
        ).resolve()
        frozen_commit = "3ef6bbd201263d354fd83ec55b3c306ded2eb72a"
        configurator_sha256 = hashlib.sha256(
            (
                script.parent / "configure_temperature.py"
            ).read_bytes()
        ).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            state_dir = root / "state"
            state_dir.mkdir()
            event_log = root / "events.txt"
            tasks_root = root / "tasks"
            generated_root = root / "generated"
            for task_name in (
                "modernize-scientific-stack",
                "overfull-hbox",
            ):
                task = tasks_root / task_name
                task.mkdir(parents=True)
                (task / "task.toml").write_text(
                    f'docker_image = "example/{task_name}:latest"\n',
                    encoding="utf-8",
                )

            docker = fake_bin / "docker"
            docker.write_text(
                f"""#!/usr/bin/env python3
import hashlib
import json
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
state_dir = Path(os.environ["FAKE_DOCKER_STATE"])
event_log = Path(os.environ["FAKE_EVENT_LOG"])

def state_path(image):
    return state_dir / (hashlib.sha256(image.encode()).hexdigest() + ".json")

def append_event(event):
    with event_log.open("a", encoding="utf-8") as stream:
        stream.write(event + "\\n")

if args[:2] == ["image", "inspect"]:
    image = args[-1]
    if (
        image == "moi/hermes-runtime:test"
        and os.environ.get("FAKE_RUNTIME_INSPECT_ERROR") == "1"
    ):
        print(
            "Cannot connect to the Docker daemon at fake.sock",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if image == "moi/hermes-runtime:test":
        data = {{
            "kind": "runtime",
            "revision": "{frozen_commit}",
            "task": "",
            "base_image": "",
            "layout": "",
            "temperature": "0.0",
            "temperature_scope": "primary_zai_chat_completions",
            "temperature_patch_sha256": "6b71f1395a6533af731c506ceaed3dab885b04055bd3bc05eae696ba9786339a",
            "temperature_patched_source_sha256": "766e0fbb7b257701323bc4a4b49697047b1b20b46f6df53d85d48314516cca0a",
            "temperature_configurator_sha256": "{configurator_sha256}",
        }}
    elif state_path(image).is_file():
        data = json.loads(state_path(image).read_text(encoding="utf-8"))
    else:
        print(
            "Error response from daemon: No such image: " + image,
            file=sys.stderr,
        )
        raise SystemExit(1)
    if "--format" in args:
        template = args[args.index("--format") + 1]
        if ".Id" in template:
            print("sha256:" + hashlib.sha256(image.encode()).hexdigest())
        elif "io.moi.hermes-tbench.kind" in template:
            print(data["kind"])
        elif "io.moi.hermes-tbench.task" in template:
            print(data["task"])
        elif "io.moi.hermes-tbench.base-image" in template:
            print(data["base_image"])
        elif "io.moi.hermes-tbench.layout" in template:
            print(data["layout"])
        elif "org.opencontainers.image.revision" in template:
            print(data["revision"])
        elif "io.moi.hermes-tbench.temperature-configurator-sha256" in template:
            print(data["temperature_configurator_sha256"])
        elif "io.moi.hermes-tbench.temperature-patched-source-sha256" in template:
            print(data["temperature_patched_source_sha256"])
        elif "io.moi.hermes-tbench.temperature-patch-sha256" in template:
            print(data["temperature_patch_sha256"])
        elif "io.moi.hermes-tbench.temperature-scope" in template:
            print(data["temperature_scope"])
        elif "io.moi.hermes-tbench.temperature" in template:
            print(data["temperature"])
    raise SystemExit(0)

if args[:2] == ["buildx", "inspect"]:
    print("Name: fake")
    print("Driver: docker")
    raise SystemExit(0)

if args[:2] == ["buildx", "build"]:
    image = args[args.index("--tag") + 1]
    task_name = ""
    base_image = ""
    layout = ""
    temperature = ""
    temperature_configurator_sha256 = ""
    for index, value in enumerate(args):
        if value != "--build-arg":
            continue
        build_arg = args[index + 1]
        if build_arg.startswith("TASK_NAME="):
            task_name = build_arg.split("=", 1)[1]
        elif build_arg.startswith("BASE_IMAGE="):
            base_image = build_arg.split("=", 1)[1]
        elif build_arg.startswith("HERMES_TASK_LAYOUT="):
            layout = build_arg.split("=", 1)[1]
        elif build_arg.startswith("HERMES_TEMPERATURE="):
            temperature = build_arg.split("=", 1)[1]
        elif build_arg.startswith("HERMES_TEMPERATURE_CONFIGURATOR_SHA256="):
            temperature_configurator_sha256 = build_arg.split("=", 1)[1]
    state_path(image).write_text(
        json.dumps({{
            "kind": "ephemeral-task",
            "revision": "{frozen_commit}",
            "task": task_name,
            "base_image": base_image,
            "layout": layout,
            "temperature": temperature,
            "temperature_scope": "primary_zai_chat_completions",
            "temperature_patch_sha256": "6b71f1395a6533af731c506ceaed3dab885b04055bd3bc05eae696ba9786339a",
            "temperature_patched_source_sha256": "766e0fbb7b257701323bc4a4b49697047b1b20b46f6df53d85d48314516cca0a",
            "temperature_configurator_sha256": temperature_configurator_sha256,
        }}),
        encoding="utf-8",
    )
    append_event("build:" + task_name)
    raise SystemExit(0)

if args[:2] == ["image", "rm"]:
    image = args[-1]
    data = json.loads(state_path(image).read_text(encoding="utf-8"))
    append_event("rm:" + data["task"])
    state_path(image).unlink()
    raise SystemExit(0)

if args and args[0] == "run":
    raise SystemExit(0)

raise SystemExit("unexpected fake docker command: " + repr(args))
""",
                encoding="utf-8",
            )
            docker.chmod(0o755)

            harbor = fake_bin / "harbor"
            harbor.write_text(
                """#!/usr/bin/env python3
import os
import sys
import time
from pathlib import Path

args = sys.argv[1:]
task_name = Path(args[args.index("--path") + 1]).name
with Path(os.environ["FAKE_EVENT_LOG"]).open(
    "a", encoding="utf-8"
) as stream:
    stream.write("harbor:" + task_name + "\\n")
if os.environ.get("FAKE_HARBOR_SLEEP") == "1":
    time.sleep(30)
raise SystemExit(17 if task_name == "modernize-scientific-stack" else 0)
""",
                encoding="utf-8",
            )
            harbor.chmod(0o755)
            config = root / "config.yaml"
            config.write_text("{}\n", encoding="utf-8")
            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env['PATH']}",
                    "FAKE_DOCKER_STATE": str(state_dir),
                    "FAKE_EVENT_LOG": str(event_log),
                    "GLM_API_KEY": "offline-placeholder",
                    "HARBOR_BIN": str(harbor),
                    "PYTHON_BIN": sys.executable,
                    "HERMES_PREBUILT_RUNTIME_IMAGE": (
                        "moi/hermes-runtime:test"
                    ),
                    "HERMES_PREBUILT_IMAGE_PREFIX": "moi/hermes-task",
                    "HERMES_PREBUILT_IMAGE_TAG": "test",
                    "HERMES_PREBUILT_LOCK_DIR": str(root / "queue.lock"),
                }
            )
            command = [
                "/bin/bash",
                str(script),
                "--config",
                str(config),
                "--tasks-root",
                str(tasks_root),
                "--generated-root",
                str(generated_root),
                "modernize-scientific-stack",
                "overfull-hbox",
            ]
            daemon_error_env = env.copy()
            daemon_error_env["FAKE_RUNTIME_INSPECT_ERROR"] = "1"
            daemon_error = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=daemon_error_env,
            )
            self.assertEqual(daemon_error.returncode, 2)
            self.assertIn(
                "unable to inspect image moi/hermes-runtime:test",
                daemon_error.stderr,
            )
            self.assertFalse((root / "queue.lock").exists())
            self.assertFalse(event_log.exists())

            signal_log = root / "signal-events.txt"
            signal_env = env.copy()
            signal_env.update(
                {
                    "FAKE_EVENT_LOG": str(signal_log),
                    "FAKE_HARBOR_SLEEP": "1",
                }
            )
            interrupted = subprocess.Popen(
                command[:-1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=signal_env,
            )
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if (
                    signal_log.exists()
                    and "harbor:modernize-scientific-stack"
                    in signal_log.read_text(encoding="utf-8")
                ):
                    break
                time.sleep(0.05)
            else:
                interrupted.kill()
                interrupted.communicate()
                self.fail("fake Harbor did not start before signal deadline")
            interrupted.send_signal(signal.SIGINT)
            interrupted_stdout, interrupted_stderr = interrupted.communicate(
                timeout=15
            )
            self.assertEqual(
                interrupted.returncode,
                130,
                interrupted_stdout + interrupted_stderr,
            )
            self.assertEqual(
                signal_log.read_text(encoding="utf-8").splitlines(),
                [
                    "build:modernize-scientific-stack",
                    "harbor:modernize-scientific-stack",
                    "rm:modernize-scientific-stack",
                ],
            )
            self.assertFalse((root / "queue.lock").exists())
            self.assertEqual(list(state_dir.iterdir()), [])

            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )

            events = event_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(result.returncode, 1)
            self.assertEqual(
                events,
                [
                    "build:modernize-scientific-stack",
                    "harbor:modernize-scientific-stack",
                    "rm:modernize-scientific-stack",
                    "build:overfull-hbox",
                    "harbor:overfull-hbox",
                    "rm:overfull-hbox",
                ],
            )
            self.assertEqual(list(state_dir.iterdir()), [])
            self.assertFalse((root / "queue.lock").exists())

    def test_policy_guard_rejects_dotenv_style_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)
            evidence = Path(directory) / "guard.jsonl"
            attacker_env = Path(directory) / "attacker.env"
            attacker_env.write_text(
                (
                    "HERMES_MANAGED_DIR=/tmp/attacker\n"
                    "HERMES_YOLO_MODE=1\n"
                    "HERMES_ACCEPT_HOOKS=1\n"
                    "API_SERVER_KEY=attacker-key\n"
                ),
                encoding="utf-8",
            )
            env = os.environ.copy()
            env.update(
                {
                    "PYTHONPATH": str(agent._policy_guard_path().parent),
                    "HERMES_C0_POLICY_GUARD_SHA256": (
                        agent._policy_guard_sha256()
                    ),
                    "HERMES_C0_POLICY_GUARD_EVIDENCE": str(evidence),
                    "HERMES_MANAGED_DIR": "/etc/hermes",
                    "HERMES_YOLO_MODE": "0",
                    "HERMES_ACCEPT_HOOKS": "0",
                    "API_SERVER_KEY": "original-api-key",
                    "ATTACKER_ENV_PATH": str(attacker_env),
                }
            )
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import json, os; "
                        "from dotenv import load_dotenv; "
                        "load_dotenv(os.environ['ATTACKER_ENV_PATH'], "
                        "override=True); "
                        "print(json.dumps({"
                        "'managed': os.environ['HERMES_MANAGED_DIR'], "
                        "'yolo': os.environ['HERMES_YOLO_MODE'], "
                        "'hooks': os.environ['HERMES_ACCEPT_HOOKS'], "
                        "'api_key': os.environ['API_SERVER_KEY']}))"
                    ),
                ],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {
                    "managed": "/etc/hermes",
                    "yolo": "0",
                    "hooks": "0",
                    "api_key": "original-api-key",
                },
            )
            rows = [
                json.loads(line)
                for line in evidence.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(rows[0]["event"], "policy_guard.loaded")
            self.assertEqual(
                rows[0]["source_sha256"],
                agent._policy_guard_sha256(),
            )

    def test_other_version_or_model_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                HermesTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name=FROZEN_MODEL_NAME,
                    version="main",
                    extra_env={"GLM_API_KEY": "offline-placeholder"},
                )
            with self.assertRaises(ValueError):
                HermesTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name=FROZEN_MODEL_NAME,
                    version=FROZEN_HERMES_VERSION,
                    max_turns=FROZEN_MAX_TURNS + 1,
                    extra_env={"GLM_API_KEY": "offline-placeholder"},
                )
            with self.assertRaises(ValueError):
                HermesTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name="zai/glm-other",
                    version=FROZEN_HERMES_VERSION,
                    extra_env={"GLM_API_KEY": "offline-placeholder"},
                )

    def test_python_bootstrap_covers_supported_task_images(self) -> None:
        self.assertIn("apt-get install -y python3", _ENSURE_PYTHON3_COMMAND)
        self.assertIn("apk add --no-cache python3", _ENSURE_PYTHON3_COMMAND)
        self.assertIn("dnf install -y python3", _ENSURE_PYTHON3_COMMAND)
        self.assertIn("yum install -y python3", _ENSURE_PYTHON3_COMMAND)

    def test_driver_command_contains_only_provider_path_not_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)
            argv = agent._gateway_driver_argv(
                instruction_path="/tmp/run/instruction.md",
                provider_env_path="/tmp/run/provider.json",
                session_id="session-1",
                gateway_port=18642,
                timeout_sec=1200,
            )

        command = " ".join(argv)
        self.assertEqual(argv[0], REMOTE_HERMES_PYTHON)
        self.assertIn("/tmp/run/provider.json", command)
        self.assertNotIn("offline-placeholder", command)
        self.assertNotIn("GLM_API_KEY", command)

    def test_workspace_fallback_is_passed_to_driver(self) -> None:
        class Environment:
            async def exec(self, command, **_kwargs):
                self.command = command
                return SimpleNamespace(return_code=0, stdout="/workspace")

        environment = Environment()
        cwd = asyncio.run(
            HermesTerminalBenchC0Agent._resolve_product_cwd(environment)
        )
        argv = HermesTerminalBenchC0Agent._gateway_driver_argv(
            instruction_path="/tmp/instruction.md",
            provider_env_path="/tmp/provider.json",
            session_id="session-1",
            gateway_port=18642,
            timeout_sec=1200,
            product_cwd=cwd,
        )

        self.assertIn("test -d /app", environment.command)
        self.assertEqual(cwd, "/workspace")
        self.assertEqual(argv[argv.index("--cwd") + 1], "/workspace")

    def test_streaming_runs_events_are_the_required_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)
            path = Path(directory) / "hermes-run-events.jsonl"
            payload = (
                '{"event":"gateway.started","pid":12,'
                '"session_id":"session-1"}\n'
                '{"event":"run.submitted","run_id":"run-1",'
                '"session_id":"session-1"}\n'
                '{"event":"run.completed","run_id":"run-1"}\n'
            )
            path.write_text(payload, encoding="utf-8")

            capture = agent._streaming_trajectory_capture(
                run_id="run-1",
                session_id="session-1",
            )

        self.assertEqual(capture["trajectory_event_stream_status"], "saved")
        self.assertEqual(capture["trajectory_event_count"], 3)
        self.assertEqual(capture["trajectory_submitted_count"], 1)
        self.assertEqual(capture["trajectory_terminal_event_count"], 1)
        self.assertEqual(
            capture["trajectory_terminal_event"], "run.completed"
        )
        self.assertEqual(
            capture["trajectory_terminal_event_source"], "hermes"
        )
        self.assertIsNone(capture["trajectory_terminal_reason"])
        self.assertEqual(
            capture["trajectory_capture_sha256"],
            hashlib.sha256(payload.encode()).hexdigest(),
        )

    def test_driver_timeout_is_a_complete_but_distinct_trajectory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)
            path = Path(directory) / "hermes-run-events.jsonl"
            path.write_text(
                (
                    '{"event":"run.submitted","run_id":"run-1",'
                    '"session_id":"session-1"}\n'
                    '{"event":"run.timed_out","run_id":"run-1",'
                    '"session_id":"session-1","source":"driver",'
                    '"reason":"ProductDeadlineExpired"}\n'
                ),
                encoding="utf-8",
            )

            capture = agent._streaming_trajectory_capture(
                run_id="run-1",
                session_id="session-1",
            )

        self.assertEqual(capture["trajectory_event_stream_status"], "saved")
        self.assertEqual(capture["trajectory_terminal_event"], "run.timed_out")
        self.assertEqual(
            capture["trajectory_terminal_event_source"], "driver"
        )
        self.assertEqual(
            capture["trajectory_terminal_reason"],
            "ProductDeadlineExpired",
        )

    def test_session_export_capture_rejects_wrong_or_empty_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)
            path = Path(directory) / "hermes-session.jsonl"
            path.write_text(
                '{"id":"wrong","messages":[{"role":"user"}]}\n',
                encoding="utf-8",
            )
            wrong = agent._session_export_capture(session_id="session-1")
            path.write_text(
                '{"id":"session-1","messages":[]}\n',
                encoding="utf-8",
            )
            empty = agent._session_export_capture(session_id="session-1")
            path.write_text(
                '{"id":"session-1","messages":[{"role":"user"}]}\n',
                encoding="utf-8",
            )
            saved = agent._session_export_capture(session_id="session-1")

        self.assertEqual(wrong["trajectory_session_export_status"], "failed")
        self.assertEqual(empty["trajectory_session_export_status"], "failed")
        self.assertEqual(saved["trajectory_session_export_status"], "saved")
        self.assertEqual(saved["trajectory_session_id"], "session-1")
        self.assertEqual(saved["trajectory_session_message_count"], 1)


class PreinstalledHermesTests(unittest.IsolatedAsyncioTestCase):
    def _agent(
        self,
        directory: str,
        *,
        preinstalled: bool,
    ) -> HermesTerminalBenchC0Agent:
        return HermesTerminalBenchC0Agent(
            logs_dir=Path(directory),
            model_name=FROZEN_MODEL_NAME,
            version=FROZEN_HERMES_VERSION,
            preinstalled=preinstalled,
            extra_env={"GLM_API_KEY": "offline-placeholder"},
        )

    async def test_prebuilt_marker_and_release_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory, preinstalled=True)
            marker_text = (
                json.dumps(agent._expected_prebuilt_marker()) + "\n"
            )

            class Environment:
                async def exec(self, command, **_kwargs):
                    if command == "cat /opt/moi/hermes-preinstalled.json":
                        return SimpleNamespace(
                            return_code=0,
                            stdout=marker_text,
                        )
                    if command.endswith("command -v hermes"):
                        return SimpleNamespace(
                            return_code=0,
                            stdout="/usr/local/bin/hermes\n",
                        )
                    if command.endswith("hermes version"):
                        return SimpleNamespace(
                            return_code=0,
                            stdout=(
                                "Hermes Agent v0.19.0 (2026.7.20)\n"
                                "Install method: git\n"
                            ),
                        )
                    if command.endswith(".git/HEAD"):
                        return SimpleNamespace(
                            return_code=0,
                            stdout=(
                                agent._expected_prebuilt_marker()[
                                    "source_commit"
                                ]
                                + "\n"
                            ),
                        )
                    raise AssertionError(command)

            await agent._verify_preinstalled_hermes(Environment())

        self.assertTrue(agent._prebuilt_marker_verified)
        self.assertEqual(
            agent._prebuilt_marker_sha256,
            hashlib.sha256(marker_text.encode()).hexdigest(),
        )

    async def test_prebuilt_marker_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory, preinstalled=True)
            marker = agent._expected_prebuilt_marker()
            marker["source_commit"] = "wrong"

            class Environment:
                async def exec(self, **_kwargs):
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps(marker),
                    )

            with self.assertRaisesRegex(
                RuntimeError,
                "does not match the frozen build",
            ):
                await agent._verify_preinstalled_hermes(Environment())

    async def test_prebuilt_live_commit_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory, preinstalled=True)

            class Environment:
                async def exec(self, command, **_kwargs):
                    if command == "cat /opt/moi/hermes-preinstalled.json":
                        return SimpleNamespace(
                            return_code=0,
                            stdout=json.dumps(
                                agent._expected_prebuilt_marker()
                            ),
                        )
                    if command.endswith("command -v hermes"):
                        return SimpleNamespace(
                            return_code=0,
                            stdout="/usr/local/bin/hermes\n",
                        )
                    if command.endswith("hermes version"):
                        return SimpleNamespace(
                            return_code=0,
                            stdout="Hermes Agent v0.19.0 (2026.7.20)\n",
                        )
                    if command.endswith(".git/HEAD"):
                        return SimpleNamespace(
                            return_code=0,
                            stdout="wrong\n",
                        )
                    raise AssertionError(command)

            with self.assertRaisesRegex(
                RuntimeError,
                "source commit does not match",
            ):
                await agent._verify_preinstalled_hermes(Environment())

    async def test_runtime_mode_still_calls_runtime_installer(self) -> None:
        class StopAfterRuntimeInstall(RuntimeError):
            pass

        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory, preinstalled=False)

            class Environment:
                async def exec(self, command, **_kwargs):
                    if command == "cat /etc/hermes/config.yaml":
                        return SimpleNamespace(
                            return_code=0,
                            stdout=agent._managed_config_path().read_text(),
                        )
                    if command == "cat /etc/hermes/.env":
                        return SimpleNamespace(
                            return_code=0,
                            stdout=agent._managed_env_path().read_text(),
                        )
                    if command == "cat /proc/self/mountinfo":
                        return SimpleNamespace(
                            return_code=0,
                            stdout=(
                                "36 25 0:32 /source /etc/hermes "
                                "ro,relatime - virtiofs source rw\n"
                            ),
                        )
                    raise StopAfterRuntimeInstall(command)

            with mock.patch(
                "astra.runners.hermes_terminal_bench.agent.Hermes.install",
                new_callable=mock.AsyncMock,
            ) as runtime_install:
                with self.assertRaises(StopAfterRuntimeInstall):
                    await agent.install(Environment())

        runtime_install.assert_awaited_once()

    async def test_preinstalled_install_skips_runtime_installer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory, preinstalled=True)
            marker_text = json.dumps(agent._expected_prebuilt_marker())

            class Environment:
                async def exec(self, command, **_kwargs):
                    if command == "cat /etc/hermes/config.yaml":
                        return SimpleNamespace(
                            return_code=0,
                            stdout=agent._managed_config_path().read_text(),
                        )
                    if command == "cat /etc/hermes/.env":
                        return SimpleNamespace(
                            return_code=0,
                            stdout=agent._managed_env_path().read_text(),
                        )
                    if command == "cat /proc/self/mountinfo":
                        return SimpleNamespace(
                            return_code=0,
                            stdout=(
                                "36 25 0:32 /source /etc/hermes "
                                "ro,relatime - virtiofs source rw\n"
                            ),
                        )
                    if command == "cat /opt/moi/hermes-preinstalled.json":
                        return SimpleNamespace(
                            return_code=0,
                            stdout=marker_text,
                        )
                    if command.endswith("command -v hermes"):
                        return SimpleNamespace(
                            return_code=0,
                            stdout="/usr/local/bin/hermes\n",
                        )
                    if command.endswith("hermes version"):
                        return SimpleNamespace(
                            return_code=0,
                            stdout="Hermes Agent v0.19.0 (2026.7.20)\n",
                        )
                    if command.endswith(".git/HEAD"):
                        return SimpleNamespace(
                            return_code=0,
                            stdout=(
                                agent._expected_prebuilt_marker()[
                                    "source_commit"
                                ]
                                + "\n"
                            ),
                        )
                    if command == "command -v python3":
                        return SimpleNamespace(return_code=0, stdout="python3")
                    if command == (
                        "cat /installed-agent/hermes-c0-policy/"
                        "sitecustomize.py"
                    ):
                        return SimpleNamespace(
                            return_code=0,
                            stdout=agent._policy_guard_path().read_text(),
                        )
                    return SimpleNamespace(
                        return_code=(
                            1 if "config set approvals.mode" in command else 0
                        ),
                        stdout=(
                            "smart"
                            if "config get approvals.mode" in command
                            else ""
                        ),
                    )

                async def upload_file(self, _source, _target):
                    return None

            agent.exec_as_agent = mock.AsyncMock(
                return_value=SimpleNamespace(return_code=0)
            )
            agent.exec_as_root = mock.AsyncMock(
                return_value=SimpleNamespace(return_code=0)
            )
            with mock.patch(
                "astra.runners.hermes_terminal_bench.agent.Hermes.install",
                new_callable=mock.AsyncMock,
            ) as runtime_install:
                await agent.install(Environment())

        runtime_install.assert_not_awaited()
        self.assertTrue(agent._prebuilt_marker_verified)


class ControllerSeamTests(unittest.IsolatedAsyncioTestCase):
    async def test_product_nonzero_is_returned_without_infra_exception(self) -> None:
        class Environment:
            async def exec(self, **_kwargs):
                return SimpleNamespace(return_code=2)

        class Controller:
            async def run(self, _environment, product_done):
                await product_done.wait()
                return "no-hit"

        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
            )
            done = asyncio.Event()
            result, error, outcome = await agent._run_with_c0_controller(
                Environment(),
                product_command="product",
                env=agent._product_env(),
                timeout_sec=1,
                controller=Controller(),
                product_done=done,
            )

        self.assertEqual(result.return_code, 2)
        self.assertIsNone(error)
        self.assertEqual(outcome, "no-hit")
        self.assertTrue(done.is_set())

    async def test_cleanup_report_is_terminal_status_authority(self) -> None:
        cleanup = {
            "schema_version": 1,
            "status": "clean",
            "reason": "normal_exit",
            "fault_action": False,
            "product_terminal_status": "failed",
            "zero_live_proven": True,
            "remaining_pids_count": 0,
            "remaining_pids": [],
        }

        class Environment:
            async def exec(self, command, **_kwargs):
                if command == "product":
                    return SimpleNamespace(return_code=1)
                if command.endswith("product.cleanup.json"):
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps(cleanup),
                    )
                if command == "cat /logs/agent/hermes-run.json":
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps({"status": "completed"}),
                    )
                raise AssertionError(command)

        class Controller:
            async def run(self, _environment, product_done):
                await product_done.wait()
                return "done"

        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
            )
            done = asyncio.Event()
            await agent._run_with_c0_controller(
                Environment(),
                product_command="product",
                env=agent._product_env(),
                timeout_sec=1,
                controller=Controller(),
                product_done=done,
                cleanup_paths={
                    "identity": "/tmp/product.identity.json",
                    "cleanup": "/tmp/product.cleanup.json",
                },
            )

        self.assertEqual(
            agent._last_process_cleanup["product_terminal_status"],
            "failed",
        )

    async def test_delayed_cleanup_report_does_not_interrupt_product_result(
        self,
    ) -> None:
        cleanup = {
            "schema_version": 1,
            "status": "clean",
            "reason": "normal_exit",
            "fault_action": False,
            "product_terminal_status": "completed",
            "zero_live_proven": True,
            "remaining_pids_count": 0,
            "remaining_pids": [],
        }

        class Environment:
            def __init__(self):
                self.cleanup_reads = 0
                self.driver_reads = 0

            async def exec(self, command, **_kwargs):
                if command == "product":
                    return SimpleNamespace(return_code=0)
                if command == "cat /logs/agent/hermes-run.json":
                    self.driver_reads += 1
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps({"status": "completed"}),
                    )
                if command.endswith("product.cleanup.json"):
                    self.cleanup_reads += 1
                    if self.cleanup_reads == 1:
                        return SimpleNamespace(return_code=1, stdout="")
                    if self.cleanup_reads == 2:
                        return SimpleNamespace(return_code=0, stdout="")
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps(cleanup),
                    )
                raise AssertionError(command)

        class Controller:
            async def run(self, _environment, product_done):
                await product_done.wait()
                return "done"

        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
            )
            environment = Environment()
            done = asyncio.Event()
            result, error, outcome = await agent._run_with_c0_controller(
                environment,
                product_command="product",
                env=agent._product_env(),
                timeout_sec=1,
                controller=Controller(),
                product_done=done,
                cleanup_paths={
                    "identity": "/tmp/product.identity.json",
                    "cleanup": "/tmp/product.cleanup.json",
                },
            )

        self.assertEqual(result.return_code, 0)
        self.assertIsNone(error)
        self.assertEqual(outcome, "done")
        self.assertEqual(environment.cleanup_reads, 3)
        self.assertEqual(environment.driver_reads, 1)
        self.assertEqual(
            agent._last_process_cleanup["product_terminal_status"],
            "completed",
        )
        self.assertTrue(done.is_set())

    async def test_cleanup_failure_does_not_override_nonzero_product_exit(
        self,
    ) -> None:
        driver_result = {
            "status": "timed_out",
            "error": "ProductDeadlineExpired: deadline",
            "run_id": "run-1",
            "session_id": "session-1",
        }

        class Environment:
            async def exec(self, command, **_kwargs):
                if command == "product":
                    return SimpleNamespace(
                        return_code=124,
                        stdout="product stdout\n",
                        stderr="product stderr\n",
                    )
                if command == "cat /logs/agent/hermes-run.json":
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps(driver_result),
                    )
                raise AssertionError(command)

        class Controller:
            async def run(self, _environment, product_done):
                await product_done.wait()
                return "done"

        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
            )
            done = asyncio.Event()
            with mock.patch(
                "astra.runners.hermes_terminal_bench.agent."
                "collect_process_cleanup_report",
                side_effect=RuntimeError("cleanup unavailable"),
            ):
                result, error, outcome = await agent._run_with_c0_controller(
                    Environment(),
                    product_command="product",
                    env=agent._product_env(),
                    timeout_sec=1,
                    controller=Controller(),
                    product_done=done,
                    cleanup_paths={
                        "identity": "/tmp/product.identity.json",
                        "cleanup": "/tmp/product.cleanup.json",
                    },
                )

            saved = json.loads(
                (Path(directory) / "hermes-run.json").read_text()
            )
            launch_stdout = (
                Path(directory) / "product-launch.stdout.txt"
            ).read_text()
            launch_stderr = (
                Path(directory) / "product-launch.stderr.txt"
            ).read_text()

        self.assertEqual(saved, driver_result)
        self.assertEqual(result.return_code, 124)
        self.assertIsNone(error)
        self.assertEqual(outcome, "done")
        self.assertEqual(agent._last_product_exec_result.return_code, 124)
        self.assertIsNone(agent._last_process_cleanup)
        self.assertEqual(
            launch_stdout,
            "product stdout\n",
        )
        self.assertEqual(
            launch_stderr,
            "product stderr\n",
        )
        self.assertTrue(done.is_set())


class HermesRunFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_required_session_export_fails_capture(self) -> None:
        class Environment:
            async def download_file(self, _source, _target):
                return None

        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
            )
            (Path(directory) / "hermes-run-events.jsonl").write_text(
                (
                    '{"event":"run.submitted","run_id":"run-1",'
                    '"session_id":"session-1"}\n'
                    '{"event":"run.completed","run_id":"run-1"}\n'
                ),
                encoding="utf-8",
            )
            agent._export_session = mock.AsyncMock()

            capture = await agent._capture_required_trajectory(
                Environment(),
                run_id="run-1",
                session_id="session-1",
            )

        self.assertEqual(capture["trajectory_event_stream_status"], "saved")
        self.assertEqual(
            capture["trajectory_session_export_status"], "failed"
        )
        self.assertEqual(capture["trajectory_capture_status"], "failed")

    async def test_failed_capture_does_not_fail_completed_product(self) -> None:
        cleanup = {
            "schema_version": 1,
            "status": "clean",
            "reason": "normal_exit",
            "fault_action": False,
            "product_terminal_status": "completed",
            "zero_live_proven": True,
            "remaining_pids_count": 0,
            "remaining_pids": [],
        }
        driver_result = {
            "status": "completed",
            "session_id": "session",
            "run_id": "run",
            "usage": {},
            "cleanup": {"fault_action": False},
            "policy_guard_active": True,
        }

        class Environment:
            async def exec(self, **_kwargs):
                return SimpleNamespace(return_code=0, stdout="", stderr="")

            async def upload_file(self, _source, _target):
                return None

        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            agent.exec_as_agent = mock.AsyncMock(
                return_value=SimpleNamespace(return_code=0)
            )
            agent._run_with_c0_controller = mock.AsyncMock(
                return_value=(
                    SimpleNamespace(return_code=0),
                    None,
                    SimpleNamespace(
                        trigger_hit=True,
                        fault_injected=False,
                        reason="trigger_hit",
                        evidence_sha256="0" * 64,
                    ),
                )
            )
            agent._last_process_cleanup = cleanup
            agent._last_process_cleanup_sha256 = "0" * 64
            agent._load_driver_result = mock.Mock(
                return_value=driver_result
            )
            agent._finalize_run_artifacts = mock.AsyncMock(
                return_value=agent._failed_trajectory_capture(
                    RuntimeError("capture unavailable")
                )
            )
            context = AgentContext()
            with mock.patch(
                "astra.runners.hermes_terminal_bench.agent.uuid.uuid4",
                side_effect=["controller-run", "session"],
            ):
                await agent.run(instruction, Environment(), context)

            rows = [
                json.loads(line)
                for line in (Path(directory) / "controller.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]

        self.assertTrue(context.metadata["product_completion_claim"])
        self.assertEqual(
            context.metadata["trajectory_capture_status"], "failed"
        )
        self.assertFalse(context.metadata["trajectory_capture_blocking"])
        self.assertEqual(rows[-1]["event"], "controller_completed")
        self.assertFalse(rows[-1]["trajectory_capture_blocking"])

    async def test_unknown_task_uses_generic_trigger_and_task_timeout(
        self,
    ) -> None:
        instruction = "Create /app/answer.txt with the requested result.\n"
        cleanup = {
            "schema_version": 1,
            "status": "clean",
            "reason": "normal_exit",
            "fault_action": False,
            "product_terminal_status": "completed",
            "zero_live_proven": True,
            "remaining_pids_count": 0,
            "remaining_pids": [],
        }
        driver_result = {
            "status": "completed",
            "session_id": "session",
            "run_id": "run",
            "usage": {},
            "cleanup": {"fault_action": False},
            "policy_guard_active": True,
        }

        class Environment:
            async def exec(self, **_kwargs):
                return SimpleNamespace(
                    return_code=0,
                    stdout="",
                    stderr="",
                )

            async def upload_file(self, _source, _target):
                return None

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
                        "agent": {"override_timeout_sec": None},
                    }
                ),
                encoding="utf-8",
            )
            agent = HermesTerminalBenchC0Agent(
                logs_dir=logs_dir,
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
                turn_timeout_sec=2000,
                trigger_timeout_sec=2000,
            )
            agent.exec_as_agent = mock.AsyncMock(
                return_value=SimpleNamespace(return_code=0)
            )
            agent._run_with_c0_controller = mock.AsyncMock(
                return_value=(
                    SimpleNamespace(return_code=0),
                    None,
                    SimpleNamespace(
                        trigger_hit=True,
                        fault_injected=False,
                        reason="trigger_hit",
                        evidence_sha256="0" * 64,
                    ),
                )
            )
            agent._last_process_cleanup = cleanup
            agent._last_process_cleanup_sha256 = "1" * 64
            agent._load_driver_result = mock.Mock(
                return_value=driver_result
            )
            agent._finalize_run_artifacts = mock.AsyncMock(
                return_value=agent._failed_trajectory_capture(
                    RuntimeError("capture unavailable")
                )
            )
            context = AgentContext()
            with mock.patch(
                "astra.runners.hermes_terminal_bench.agent.uuid.uuid4",
                side_effect=["controller-run", "session"],
            ):
                await agent.run(instruction, Environment(), context)

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
            1800.0,
        )
        self.assertEqual(context.metadata["product_timeout_sec"], 1800.0)
        self.assertTrue(context.metadata["lifecycle_gate_passed"])

    async def test_session_export_is_bound_to_current_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
            )
            agent.exec_as_agent = mock.AsyncMock(
                return_value=SimpleNamespace(return_code=0)
            )
            await agent._export_session(
                SimpleNamespace(),
                session_id="session-1",
            )

        command = agent.exec_as_agent.await_args.kwargs["command"]
        self.assertIn(
            "hermes sessions export "
            "/logs/agent/hermes-session.jsonl --session-id session-1",
            command,
        )
        self.assertNotIn("--source", command)
        self.assertNotIn("|| true", command)

    async def test_self_cancelled_finalizer_is_a_capture_failure(self) -> None:
        async def cancel_itself() -> dict[str, object]:
            raise asyncio.CancelledError

        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
            )
            capture, pending_cancel = (
                await agent._await_trajectory_finalizer(
                    asyncio.create_task(cancel_itself())
                )
            )

        self.assertEqual(capture["trajectory_capture_status"], "failed")
        self.assertIsNone(pending_cancel)

    async def test_required_export_cancellation_leaves_terminal_ledger(self) -> None:
        identity = {
            "pid": 1234,
            "ppid": 1200,
            "pgid": 1234,
            "sid": 1234,
            "start_ticks": 99,
            "exe": "/usr/bin/python3",
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
        cleanup = {
            "schema_version": 1,
            "status": "clean",
            "reason": "normal_exit",
            "fault_action": False,
            "product_terminal_status": "completed",
            "zero_live_proven": True,
            "remaining_pids_count": 0,
            "remaining_pids": [],
        }
        driver_result = {
            "status": "completed",
            "session_id": "session",
            "run_id": "run",
            "usage": {},
            "cleanup": {"fault_action": False},
        }
        predicate_id = (
            "terminal-bench.modernize-scientific-stack.partial-outputs"
        )

        class Environment:
            def __init__(self):
                self.optional_logs_started = asyncio.Event()

            async def exec(self, command, **_kwargs):
                if "lifecycle-process-probe.py run" in command:
                    await asyncio.sleep(0.03)
                    return SimpleNamespace(return_code=0, stdout="", stderr="")
                if command.startswith("cat ") and command.endswith(
                    "product.cleanup.json"
                ):
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps(cleanup),
                        stderr="",
                    )
                if command == "cat /logs/agent/hermes-run.json":
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps(driver_result),
                        stderr="",
                    )
                if command.startswith("cat ") and command.endswith(
                    "product.identity.json"
                ):
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps(identity),
                        stderr="",
                    )
                if "lifecycle-predicate-probe.py" in command:
                    return SimpleNamespace(
                        return_code=0,
                        stdout=json.dumps(
                            {
                                "schema_version": 1,
                                "predicate_id": predicate_id,
                                "matched": True,
                                "evidence": {"state": "partial"},
                            }
                        ),
                        stderr="",
                    )
                return SimpleNamespace(return_code=0, stdout="", stderr="")

            async def upload_file(self, _source, _target):
                return None

            async def download_file(self, source, target):
                path = Path(target)
                path.parent.mkdir(parents=True, exist_ok=True)
                if source.endswith("hermes-run-events.jsonl"):
                    path.write_text(
                        (
                            '{"event":"gateway.started","session_id":"session"}\n'
                            '{"event":"run.submitted","run_id":"run",'
                            '"session_id":"session"}\n'
                            '{"event":"run.completed","run_id":"run"}\n'
                        ),
                        encoding="utf-8",
                    )
                    return
                if source.endswith("hermes-session.jsonl"):
                    self.optional_logs_started.set()
                    await asyncio.sleep(0.05)
                    path.write_text(
                        (
                            '{"id":"session","messages":'
                            '[{"role":"user","content":"task"}]}\n'
                        ),
                        encoding="utf-8",
                    )
                    return
                if source.endswith("hermes-run.json"):
                    path.write_text(
                        json.dumps(driver_result),
                        encoding="utf-8",
                    )
                    return
                path.write_text("", encoding="utf-8")

        instruction = Path(
            "work/terminal-bench-2-1/tasks/"
            "modernize-scientific-stack/instruction.md"
        ).read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            agent = HermesTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_HERMES_VERSION,
                extra_env={"GLM_API_KEY": "offline-placeholder"},
                trigger_timeout_sec=1,
                poll_interval_sec=0.001,
                turn_timeout_sec=10,
            )
            environment = Environment()
            with mock.patch(
                "astra.runners.hermes_terminal_bench.agent.uuid.uuid4",
                side_effect=["controller-run", "session"],
            ):
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
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(rows[-1]["event"], "controller_completed")
            self.assertEqual(
                rows[-1]["trajectory_capture_status"], "saved"
            )
            self.assertEqual(
                rows[-1]["trajectory_session_export_status"], "saved"
            )
            cleanup_event = next(
                row for row in rows if row["event"] == "product_process_cleanup"
            )
            self.assertTrue(cleanup_event["zero_live_proven"])
