from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harbor.models.agent.context import AgentContext

from astra.runners.dsh_terminal_bench.agent import (
    DEEPSEEK_NATIVE_PROFILE_ID,
    DshSessionError,
    DshTerminalBenchS0Agent,
    DshTerminalBenchC0Agent,
    TERMINAL_BENCH_DEEPSEEK_V4_FLASH_MAX_PROFILE_ID,
    TERMINAL_BENCH_GLM52_PROFILE_ID,
    _ENSURE_PYTHON3_COMMAND,
)
from astra.runners.dsh_terminal_bench.install_runtime import DSH_RUNTIME_VERSION


class DshAgentTests(unittest.TestCase):
    def _agent(self, logs_dir: Path) -> DshTerminalBenchS0Agent:
        return DshTerminalBenchS0Agent(
            logs_dir=logs_dir,
            model_name="deepseek-official/deepseek-v4-flash",
            profile=DEEPSEEK_NATIVE_PROFILE_ID,
            version=DSH_RUNTIME_VERSION,
            extra_env={"DEEPSEEK_API_KEY": "test-secret"},
        )

    def test_secret_is_removed_from_setup_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(Path(directory))
            self.assertNotIn("DEEPSEEK_API_KEY", agent.extra_env)
            self.assertEqual(
                agent._product_env()["DEEPSEEK_API_KEY"], "test-secret"
            )

    def test_python_install_preserves_ca_less_apt_sources(self) -> None:
        self.assertNotIn("s|http://|https://|g", _ENSURE_PYTHON3_COMMAND)
        self.assertIn("apt-get update", _ENSURE_PYTHON3_COMMAND)
        self.assertIn("install -y python3 ca-certificates", _ENSURE_PYTHON3_COMMAND)

    def test_runtime_wheel_path_comes_from_agent_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "runtime.whl"
            wheel.write_bytes(b"runtime")
            agent = DshTerminalBenchC0Agent(
                logs_dir=root / "logs",
                model_name="zai/glm-5.2",
                profile=TERMINAL_BENCH_GLM52_PROFILE_ID,
                version=DSH_RUNTIME_VERSION,
                extra_env={
                    "ZAI_API_KEY": "zai-secret",
                    "DSH_RUNTIME_WHEEL": str(wheel),
                },
            )
            self.assertEqual(agent.runtime_wheel_path, wheel)
            self.assertNotIn("DSH_RUNTIME_WHEEL", agent.extra_env)

    def test_applies_native_token_buckets_to_harbor_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            logs_dir = Path(directory)
            (logs_dir / "dsh-events.jsonl").write_text("{}\n", encoding="utf-8")
            agent = self._agent(logs_dir)
            context = AgentContext()
            result = {
                "schema_version": 1,
                "status": "completed",
                "session_id": "session-1",
                "finish_reason": "completed",
                "event_count": 4,
                "turn_count": 1,
                "step_count": 1,
                "tool_call_count": 1,
                "usage": {
                    "fresh_input_tokens": 11,
                    "cache_read_tokens": 5,
                    "cache_write_tokens": 3,
                    "output_tokens": 7,
                    "reasoning_tokens": 2,
                },
            }
            metadata = agent._apply_result(context, result, {"condition": "S0"})
            self.assertEqual(context.n_input_tokens, 16)
            self.assertEqual(context.n_cache_tokens, 5)
            self.assertEqual(context.n_output_tokens, 7)
            self.assertEqual(metadata["dsh_usage"]["cache_write_tokens"], 3)
            self.assertEqual(len(metadata["dsh_trajectory_sha256"]), 64)

    def test_rejects_dsh_error_finish_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(Path(directory))
            with self.assertRaisesRegex(
                DshSessionError, "dsh_finish_reason='error'"
            ):
                agent._require_successful_session(
                    {"status": "error", "finish_reason": "error"}
                )

    def test_terminalbench_profile_selects_glm52_route_and_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = DshTerminalBenchS0Agent(
                logs_dir=Path(directory),
                model_name="zai/glm-5.2",
                profile=TERMINAL_BENCH_GLM52_PROFILE_ID,
                version=DSH_RUNTIME_VERSION,
                extra_env={"ZAI_API_KEY": "zai-secret"},
            )
            self.assertNotIn("ZAI_API_KEY", agent.extra_env)
            self.assertEqual(
                agent._config_path().name, "minimal-pi-ai-glm52.cordis.yml"
            )
            self.assertEqual(agent.max_turns, 50)
            self.assertEqual(agent.temperature, 0.0)
            self.assertEqual(agent.reasoning_effort, "high")
            self.assertIsNone(agent.max_tokens)
            self.assertEqual(agent._product_env()["ZAI_API_KEY"], "zai-secret")
            self.assertEqual(
                agent._product_env()["DSH_REASONING_EFFORT"], "high"
            )

            argv = agent._driver_argv(
                instruction_file="/tmp/instruction.md",
                result_file="/tmp/result.json",
                events_file="/tmp/events.jsonl",
                runtime_stderr_file="/tmp/runtime.stderr",
                session_root="/tmp/sessions",
                session_id="test-session",
                cwd="/tmp",
                timeout_sec=10,
            )
            self.assertEqual(argv[argv.index("--provider") + 1], "zai")
            self.assertEqual(argv[argv.index("--model") + 1], "glm-5.2")
            self.assertEqual(argv[argv.index("--max-turns") + 1], "50")
            self.assertNotIn("--max-tokens", argv)

    def test_profile_rejects_a_mismatched_harbor_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "requires model zai/glm-5.2"):
                DshTerminalBenchS0Agent(
                    logs_dir=Path(directory),
                    model_name="deepseek-official/deepseek-v4-flash",
                    profile=TERMINAL_BENCH_GLM52_PROFILE_ID,
                    version=DSH_RUNTIME_VERSION,
                    extra_env={"ZAI_API_KEY": "zai-secret"},
                )

    def test_c0_product_timeout_multiplier_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            default_agent = DshTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="zai/glm-5.2",
                profile=TERMINAL_BENCH_GLM52_PROFILE_ID,
                version=DSH_RUNTIME_VERSION,
                extra_env={"ZAI_API_KEY": "zai-secret"},
            )
            self.assertEqual(default_agent.product_timeout_multiplier, 2.0)
            agent = DshTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name="zai/glm-5.2",
                profile=TERMINAL_BENCH_GLM52_PROFILE_ID,
                version=DSH_RUNTIME_VERSION,
                product_timeout_multiplier=1.0,
                extra_env={"ZAI_API_KEY": "zai-secret"},
            )
            self.assertEqual(agent.product_timeout_multiplier, 1.0)

            with self.assertRaisesRegex(ValueError, "must be positive"):
                DshTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name="zai/glm-5.2",
                    profile=TERMINAL_BENCH_GLM52_PROFILE_ID,
                    version=DSH_RUNTIME_VERSION,
                    product_timeout_multiplier=0,
                    extra_env={"ZAI_API_KEY": "zai-secret"},
                )

    def test_terminalbench_deepseek_flash_max_profile_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = DshTerminalBenchS0Agent(
                logs_dir=Path(directory),
                model_name="deepseek-official/deepseek-v4-flash",
                profile=TERMINAL_BENCH_DEEPSEEK_V4_FLASH_MAX_PROFILE_ID,
                version=DSH_RUNTIME_VERSION,
                extra_env={"DEEPSEEK_API_KEY": "deepseek-secret"},
            )
            self.assertEqual(
                agent._config_path().name,
                "minimal-deepseek-v4-flash-max.cordis.yml",
            )
            self.assertEqual(agent.max_turns, 50)
            self.assertEqual(agent.temperature, 0.0)
            self.assertEqual(agent.max_tokens, 49_152)
            self.assertEqual(agent.reasoning_effort, "max")
            self.assertEqual(
                agent._product_env(),
                {
                    "DEEPSEEK_API_KEY": "deepseek-secret",
                    "DSH_MODEL": "deepseek-v4-flash",
                    "DSH_CONTEXT_WINDOW": "1000000",
                    "DSH_MAX_TURNS": "50",
                    "DSH_TEMPERATURE": "0.0",
                    "DSH_MAX_TOKENS": "49152",
                    "DSH_REASONING_EFFORT": "max",
                },
            )

            argv = agent._driver_argv(
                instruction_file="/tmp/instruction.md",
                result_file="/tmp/result.json",
                events_file="/tmp/events.jsonl",
                runtime_stderr_file="/tmp/runtime.stderr",
                session_root="/tmp/sessions",
                session_id="test-session",
                cwd="/tmp",
                timeout_sec=10,
            )
            self.assertEqual(
                argv[argv.index("--provider") + 1], "deepseek-official"
            )
            self.assertEqual(
                argv[argv.index("--model") + 1], "deepseek-v4-flash"
            )
            self.assertEqual(argv[argv.index("--max-turns") + 1], "50")
            self.assertEqual(argv[argv.index("--max-tokens") + 1], "49152")


if __name__ == "__main__":
    unittest.main()
