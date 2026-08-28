from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from astra.runners.dsh_terminal_bench.install_runtime import (
    RUNTIME_ARTIFACTS,
    _runtime_member,
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


if __name__ == "__main__":
    unittest.main()
