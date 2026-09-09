from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from astra.runners.linux_terminal_bench.products import PRODUCTS
from astra.runners.linux_terminal_bench.run import (
    _base_environment,
    _container_proxy_url,
)


class LinuxConfigTests(unittest.TestCase):
    def test_all_products_use_one_x_and_common_evidence_verifier(self) -> None:
        for product in PRODUCTS.values():
            with self.subTest(product=product.id):
                content = product.config_path.read_text(encoding="utf-8")
                self.assertIn("timeout_multiplier: 1.0", content.splitlines())
                self.assertIn("product_timeout_multiplier: 1.0", content)
                self.assertIn("TerminalBenchEvidenceVerifier", content)
                self.assertIn("agent_timeout_multiplier: 1.25", content)
                self.assertIn("verifier_timeout_multiplier: 2.0", content)
                self.assertIn("docker-compose-dns.yaml", content)
                self.assertIn("HTTP_PROXY: ${TBENCH_VERIFIER_PROXY}", content)
                self.assertIn("https_proxy: ${TBENCH_VERIFIER_PROXY}", content)
                self.assertIn('ALL_PROXY: ""', content)
                self.assertIn("pypi.org,files.pythonhosted.org", content)
                if product.id == "dsh":
                    self.assertIn("DSH_RUNTIME_WHEEL: ${DSH_RUNTIME_WHEEL}", content)

    def test_loopback_proxy_is_reachable_from_task_containers(self) -> None:
        self.assertEqual(
            _container_proxy_url("http://127.0.0.1:7890"),
            "http://host.docker.internal:7890",
        )
        self.assertEqual(
            _container_proxy_url("http://proxy.example:8080"),
            "http://proxy.example:8080",
        )

    def test_hermes_build_uses_the_host_proxy_url(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"HTTPS_PROXY": "http://127.0.0.1:7890"},
            clear=True,
        ):
            env = _base_environment(
                Path("/repo"),
                Path("/data"),
                "/harbor",
                "/python",
            )

        self.assertEqual(
            env["HERMES_TBENCH_BUILD_PROXY"],
            "http://127.0.0.1:7890",
        )
        self.assertEqual(
            env["TBENCH_VERIFIER_PROXY"],
            "http://host.docker.internal:7890",
        )

    def test_all_products_use_glm52_thinking_high(self) -> None:
        self.assertEqual(
            PRODUCTS["astra"].model,
            "glm-5.2(thinking:high)",
        )
        self.assertEqual(
            PRODUCTS["hermes"].model,
            "zai/glm-5.2",
        )
        self.assertEqual(
            PRODUCTS["pi"].model,
            "zai/glm-5.2",
        )
        self.assertEqual(
            PRODUCTS["dsh"].model,
            "zai/glm-5.2",
        )
        for product in PRODUCTS.values():
            with self.subTest(product=product.id):
                content = product.config_path.read_text(encoding="utf-8")
                self.assertIn("glm-5.2", content.lower())
        self.assertIn(
            "thinking: high",
            PRODUCTS["pi"].config_path.read_text(encoding="utf-8"),
        )
        self.assertIn(
            "terminalbench-glm52",
            PRODUCTS["dsh"].config_path.read_text(encoding="utf-8"),
        )


if __name__ == "__main__":
    unittest.main()
