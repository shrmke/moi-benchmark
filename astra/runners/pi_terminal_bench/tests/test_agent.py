from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from astra.runners.pi_terminal_bench.agent import (
    DEEPSEEK_MODEL_NAME,
    FROZEN_MODEL_NAME,
    FROZEN_PI_VERSION,
    PiTerminalBenchC0Agent,
    _managed_models_path,
)


class PiC0AgentTests(unittest.TestCase):
    def _agent(self, directory: str) -> PiTerminalBenchC0Agent:
        return PiTerminalBenchC0Agent(
            logs_dir=Path(directory),
            model_name=FROZEN_MODEL_NAME,
            version=FROZEN_PI_VERSION,
            preinstalled=True,
            extra_env={"ZAI_API_KEY": "offline-placeholder"},
        )

    def test_freezes_version_model_and_key_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = self._agent(directory)

        self.assertEqual(agent.version(), "0.73.1")
        self.assertEqual(agent.model_name, "zai/glm-5.2")
        self.assertNotIn("ZAI_API_KEY", agent._extra_env)
        self.assertEqual(
            agent.extra_env["ZAI_API_KEY"], "offline-placeholder"
        )
        self.assertEqual(
            agent._product_env()["ZAI_API_KEY"], "offline-placeholder"
        )

    def test_models_profile_is_frozen_to_glm_5_2(self) -> None:
        content = _managed_models_path().read_text(encoding="utf-8")

        self.assertIn('"glm-5.2"', content)
        self.assertIn('"ZAI_API_KEY"', content)

    def test_deepseek_flash_uses_native_max_effort_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            agent = PiTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=DEEPSEEK_MODEL_NAME,
                version=FROZEN_PI_VERSION,
                preinstalled=True,
                thinking="xhigh",
                extra_env={"DEEPSEEK_API_KEY": "offline-placeholder"},
            )

        self.assertEqual(agent._provider, "deepseek")
        self.assertEqual(agent._model, "deepseek-v4-flash")
        self.assertEqual(agent._reasoning_effort, "max")
        self.assertEqual(agent.build_cli_flags(), "--thinking xhigh")
        self.assertEqual(
            agent._product_env()["DEEPSEEK_API_KEY"],
            "offline-placeholder",
        )
        self.assertNotIn("DEEPSEEK_API_KEY", agent._extra_env)

    def test_product_timeout_multiplier_is_configurable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(self._agent(directory).product_timeout_multiplier, 2.0)
            agent = PiTerminalBenchC0Agent(
                logs_dir=Path(directory),
                model_name=FROZEN_MODEL_NAME,
                version=FROZEN_PI_VERSION,
                preinstalled=True,
                product_timeout_multiplier=1.0,
                extra_env={"ZAI_API_KEY": "offline-placeholder"},
            )
            self.assertEqual(agent.product_timeout_multiplier, 1.0)

            with self.assertRaisesRegex(ValueError, "must be positive"):
                PiTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name=FROZEN_MODEL_NAME,
                    version=FROZEN_PI_VERSION,
                    preinstalled=True,
                    product_timeout_multiplier=0,
                    extra_env={"ZAI_API_KEY": "offline-placeholder"},
                )

    def test_rejects_wrong_version_or_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "requires version"):
                PiTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name=FROZEN_MODEL_NAME,
                    version="latest",
                    preinstalled=True,
                    extra_env={"ZAI_API_KEY": "offline-placeholder"},
                )
            with self.assertRaisesRegex(ValueError, "requires model"):
                PiTerminalBenchC0Agent(
                    logs_dir=Path(directory),
                    model_name="zai/glm-5.1",
                    version=FROZEN_PI_VERSION,
                    preinstalled=True,
                    extra_env={"ZAI_API_KEY": "offline-placeholder"},
                )
