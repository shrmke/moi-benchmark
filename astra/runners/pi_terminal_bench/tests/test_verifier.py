from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call, patch

from harbor.models.task.config import TaskOS
from harbor.verifier.verifier import Verifier

from astra.runners.pi_terminal_bench.verifier import (
    TerminalBenchEvidenceVerifier,
    VerifierInfrastructureError,
)


class PiVerifierTests(unittest.IsolatedAsyncioTestCase):
    def _verifier(self, verifier_dir: Path) -> TerminalBenchEvidenceVerifier:
        verifier = object.__new__(TerminalBenchEvidenceVerifier)
        verifier.trial_paths = SimpleNamespace(verifier_dir=verifier_dir)
        verifier.environment = SimpleNamespace(
            os=TaskOS.LINUX,
            exec=AsyncMock(
                return_value=SimpleNamespace(
                    return_code=0, stdout=None, stderr=None
                )
            ),
        )
        return verifier

    def _cached_verifier(
        self, cache_root: Path, test_script: Path
    ) -> TerminalBenchEvidenceVerifier:
        environment = SimpleNamespace(
            os=SimpleNamespace(value="linux"),
            exec=AsyncMock(
                return_value=SimpleNamespace(
                    return_code=0, stdout="/custom/bin:/usr/bin", stderr=None
                )
            ),
            upload_file=AsyncMock(),
        )
        verifier = object.__new__(TerminalBenchEvidenceVerifier)
        verifier._cache_root = str(cache_root)
        verifier.task = SimpleNamespace(
            paths=SimpleNamespace(
                discovered_test_path_for=lambda _os: test_script
            )
        )
        verifier.environment = environment
        verifier.override_env = {}
        verifier.logger = Mock()
        verifier._resolve_tests = Mock(
            return_value=([test_script.parent], test_script.parent, test_script)
        )
        return verifier

    @staticmethod
    def _write_cache(cache_root: Path, python_archive: str) -> None:
        (cache_root / "bin").mkdir(parents=True)
        (cache_root / "wheels").mkdir(parents=True)
        (cache_root / "python-build-standalone" / "20251014").mkdir(
            parents=True
        )
        for name in ("uv", "uvx", "curl"):
            (cache_root / "bin" / name).write_bytes(b"cached")
        (
            cache_root
            / "python-build-standalone"
            / "20251014"
            / python_archive
        ).write_bytes(b"cached")
        (cache_root / "wheels" / "pytest-8.4.1-py3-none-any.whl").write_bytes(
            b"cached"
        )
        (
            cache_root
            / "wheels"
            / "pytest_json_ctrf-0.3.5-py3-none-any.whl"
        ).write_bytes(b"cached")

    async def test_rejects_reward_when_pytest_did_not_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            Verifier,
            "verify",
            new=AsyncMock(
                return_value=SimpleNamespace(rewards={"reward": 0.0})
            ),
        ):
            verifier = self._verifier(Path(directory))

            with self.assertRaisesRegex(
                VerifierInfrastructureError,
                "did not produce ctrf.json",
            ):
                await verifier.verify()

    async def test_returns_reward_after_real_test_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            Verifier,
            "verify",
            new=AsyncMock(
                return_value=SimpleNamespace(rewards={"reward": 1.0})
            ),
        ):
            verifier_dir = Path(directory)
            (verifier_dir / "ctrf.json").write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {"tests": 1},
                            "tests": [
                                {"name": "test_answer", "status": "passed"}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            verifier = self._verifier(verifier_dir)

            result = await verifier.verify()

        self.assertEqual(result.rewards, {"reward": 1.0})
        permission_command = verifier.environment.exec.await_args.kwargs
        self.assertEqual(permission_command["user"], "root")
        self.assertIn(
            "chmod 0644 /logs/verifier/ctrf.json",
            permission_command["command"],
        )

    async def test_rejects_non_binary_reward(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            Verifier,
            "verify",
            new=AsyncMock(
                return_value=SimpleNamespace(rewards={"reward": 0.5})
            ),
        ):
            verifier_dir = Path(directory)
            (verifier_dir / "ctrf.json").write_text(
                json.dumps(
                    {
                        "results": {
                            "summary": {"tests": 1},
                            "tests": [
                                {"name": "test_answer", "status": "failed"}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            verifier = self._verifier(verifier_dir)

            with self.assertRaisesRegex(
                VerifierInfrastructureError,
                "finite number 0 or 1",
            ):
                await verifier.verify()

    async def test_injects_uv_and_requested_python_after_agent_phase(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_root = root / "cache"
            test_script = root / "test.sh"
            test_script.write_text(
                "curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh\n"
                "uvx -p 3.11 -w pytest pytest\n",
                encoding="utf-8",
            )
            archive = (
                "cpython-3.11.14+20251014-x86_64-unknown-linux-gnu-"
                "install_only_stripped.tar.gz"
            )
            self._write_cache(cache_root, archive)
            verifier = self._cached_verifier(cache_root, test_script)

            await verifier._prepare_bootstrap_cache()
            resolved_cache_root = cache_root.resolve()

        self.assertEqual(verifier.environment.exec.await_count, 3)
        self.assertIn(
            "rm -rf -- /tmp/moi-pi-verifier-bootstrap",
            verifier.environment.exec.await_args_list[0].kwargs["command"],
        )
        self.assertIn(
            "python install 3.11",
            verifier.environment.exec.await_args_list[2].kwargs["command"],
        )
        verifier.environment.upload_file.assert_has_awaits(
            [
                call(
                    resolved_cache_root / "bin" / "uv",
                    "/tmp/moi-pi-verifier-bootstrap/bin/uv",
                ),
                call(
                    resolved_cache_root / "bin" / "uvx",
                    "/tmp/moi-pi-verifier-bootstrap/bin/uvx",
                ),
                call(
                    resolved_cache_root / "bin" / "curl",
                    "/tmp/moi-pi-verifier-bootstrap/bin/curl",
                ),
                call(
                    resolved_cache_root
                    / "python-build-standalone"
                    / "20251014"
                    / archive,
                    "/tmp/moi-pi-verifier-bootstrap/python-build-standalone/"
                    f"20251014/{archive}",
                ),
            ]
        )
        self.assertEqual(
            verifier.override_env["UV_PYTHON_INSTALL_MIRROR"],
            "file:///tmp/moi-pi-verifier-bootstrap/python-build-standalone",
        )
        self.assertEqual(
            verifier.override_env["UV_FIND_LINKS"],
            "file:///tmp/moi-pi-verifier-bootstrap/wheels",
        )
        verifier.environment.upload_file.assert_any_await(
            resolved_cache_root / "wheels" / "pytest-8.4.1-py3-none-any.whl",
            "/tmp/moi-pi-verifier-bootstrap/wheels/"
            "pytest-8.4.1-py3-none-any.whl",
        )
        self.assertTrue(
            verifier.override_env["PATH"]
            == "/tmp/moi-pi-verifier-bootstrap/bin:/custom/bin:/usr/bin"
        )

    async def test_missing_python_archive_is_infrastructure_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_root = root / "cache"
            test_script = root / "test.sh"
            test_script.write_text(
                "curl -LsSf https://astral.sh/uv/0.9.5/install.sh | sh\n"
                "uvx -p 3.13 pytest\n",
                encoding="utf-8",
            )
            (cache_root / "bin").mkdir(parents=True)
            for name in ("uv", "uvx", "curl"):
                (cache_root / "bin" / name).write_bytes(b"cached")
            verifier = self._cached_verifier(cache_root, test_script)

            with self.assertRaisesRegex(
                VerifierInfrastructureError,
                "missing verifier bootstrap cache file",
            ):
                await verifier._prepare_bootstrap_cache()

        verifier.environment.exec.assert_not_awaited()
        verifier.environment.upload_file.assert_not_awaited()

    async def test_injects_cache_for_uvx_without_installer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_root = root / "cache"
            test_script = root / "test.sh"
            test_script.write_text(
                "uvx -p 3.13 -w pytest pytest\n",
                encoding="utf-8",
            )
            archive = (
                "cpython-3.13.9+20251014-x86_64-unknown-linux-gnu-"
                "install_only_stripped.tar.gz"
            )
            self._write_cache(cache_root, archive)
            verifier = self._cached_verifier(cache_root, test_script)

            await verifier._prepare_bootstrap_cache()

        verifier.environment.upload_file.assert_awaited()
        self.assertEqual(
            verifier.override_env["UV_PYTHON_INSTALL_MIRROR"],
            "file:///tmp/moi-pi-verifier-bootstrap/python-build-standalone",
        )

    async def test_ctrf_only_uvx_uses_offline_wheelhouse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache_root = root / "cache"
            test_script = root / "test.sh"
            test_script.write_text(
                "uvx -p 3.13 -w pytest==8.4.1 "
                "-w pytest-json-ctrf==0.3.5 pytest\n",
                encoding="utf-8",
            )
            archive = (
                "cpython-3.13.9+20251014-x86_64-unknown-linux-gnu-"
                "install_only_stripped.tar.gz"
            )
            self._write_cache(cache_root, archive)
            verifier = self._cached_verifier(cache_root, test_script)

            await verifier._prepare_bootstrap_cache()

        self.assertEqual(verifier.override_env["UV_OFFLINE"], "1")
