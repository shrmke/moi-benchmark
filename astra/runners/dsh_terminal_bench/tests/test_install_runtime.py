from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from astra.runners.dsh_terminal_bench.install_runtime import (
    RUNTIME_ARTIFACTS,
    RuntimeArtifact,
    _download_runtime,
    _runtime_member,
    ensure_runtime_wheel,
    install_runtime,
    normalize_machine,
    select_runtime,
)


class RuntimeInstallerTests(unittest.TestCase):
    def test_normalizes_supported_architectures(self) -> None:
        self.assertEqual(normalize_machine("AMD64"), "x86_64")
        self.assertEqual(normalize_machine("arm64"), "aarch64")
        self.assertEqual(select_runtime("x64"), RUNTIME_ARTIFACTS["x86_64"])

    def test_rejects_unsupported_architecture(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "does not support"):
            select_runtime("riscv64")

    def test_finds_exact_runtime_member(self) -> None:
        artifact = RUNTIME_ARTIFACTS["x86_64"]
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "runtime.whl"
            expected = "pkg/dsh-jsonrpc-agent-pkg-linux-x64"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(expected, b"runtime")
                archive.writestr("pkg/other", b"other")
            with zipfile.ZipFile(wheel) as archive:
                self.assertEqual(_runtime_member(archive, artifact), expected)

    def test_retries_transient_download_failure(self) -> None:
        artifact = RUNTIME_ARTIFACTS["x86_64"]
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / artifact.filename
            with (
                mock.patch(
                    "urllib.request.urlopen",
                    side_effect=[OSError("temporary failure"), io.BytesIO(b"runtime")],
                ) as urlopen,
                mock.patch("time.sleep") as sleep,
            ):
                _download_runtime(artifact, wheel)
            self.assertEqual(wheel.read_bytes(), b"runtime")
            self.assertEqual(urlopen.call_count, 2)
            sleep.assert_called_once_with(1)

    def test_reuses_verified_host_wheel_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            wheel = Path(directory) / "runtime.whl"
            wheel.write_bytes(b"cached-runtime")
            artifact = RuntimeArtifact(
                arch="x86_64",
                filename=wheel.name,
                url="https://unused.invalid/runtime.whl",
                sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                binary_suffix="linux-x64",
            )
            with (
                mock.patch(
                    "astra.runners.dsh_terminal_bench.install_runtime.select_runtime",
                    return_value=artifact,
                ),
                mock.patch(
                    "astra.runners.dsh_terminal_bench.install_runtime._download_runtime"
                ) as download,
            ):
                self.assertEqual(ensure_runtime_wheel(wheel), artifact)
            download.assert_not_called()

    def test_installs_from_local_wheel_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "runtime.whl"
            member = "pkg/dsh-jsonrpc-agent-pkg-linux-x64"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr(member, b"runtime")
            artifact = RuntimeArtifact(
                arch="x86_64",
                filename=wheel.name,
                url="https://unused.invalid/runtime.whl",
                sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
                binary_suffix="linux-x64",
            )
            destination = root / "installed"
            with (
                mock.patch(
                    "astra.runners.dsh_terminal_bench.install_runtime.select_runtime",
                    return_value=artifact,
                ),
                mock.patch("urllib.request.urlopen") as urlopen,
            ):
                marker = install_runtime(destination, wheel_path=wheel)
            urlopen.assert_not_called()
            self.assertEqual(
                (destination / "dsh-jsonrpc-agent").read_bytes(), b"runtime"
            )
            self.assertEqual(marker["wheel_sha256"], artifact.sha256)


if __name__ == "__main__":
    unittest.main()
