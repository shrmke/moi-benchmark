#!/usr/bin/env python3
"""Install one frozen DSH runtime wheel without depending on pip."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import platform
import shutil
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


DSH_RUNTIME_VERSION = "0.1.0rc6"


@dataclass(frozen=True)
class RuntimeArtifact:
    arch: str
    filename: str
    url: str
    sha256: str
    binary_suffix: str


RUNTIME_ARTIFACTS = {
    "x86_64": RuntimeArtifact(
        arch="x86_64",
        filename=(
            "deepseek_harness_runtime_bin-0.1.0rc6-"
            "py3-none-manylinux_2_28_x86_64.whl"
        ),
        url=(
            "https://files.pythonhosted.org/packages/c6/26/"
            "1c6bc9c26618ca5b2f155a428d4c0c5d5174b68077cccbc10a1629bb3611/"
            "deepseek_harness_runtime_bin-0.1.0rc6-"
            "py3-none-manylinux_2_28_x86_64.whl"
        ),
        sha256="d7261d3bdadfa8d10ab03fd06c6bbc66a182ae27d39892a0eb7c2ce9d63a5448",
        binary_suffix="linux-x64",
    ),
    "aarch64": RuntimeArtifact(
        arch="aarch64",
        filename=(
            "deepseek_harness_runtime_bin-0.1.0rc6-"
            "py3-none-manylinux_2_28_aarch64.whl"
        ),
        url=(
            "https://files.pythonhosted.org/packages/f6/0b/"
            "d9128f0b3edc5ab5fc6d8746e669c924bd23fd017e645796b3b439dd0eb3/"
            "deepseek_harness_runtime_bin-0.1.0rc6-"
            "py3-none-manylinux_2_28_aarch64.whl"
        ),
        sha256="99d0ef334a4e3cb178d7b0302bbdd01c8dde6068ee5fe8b01e074541db5c7747",
        binary_suffix="linux-arm64",
    ),
}

DOWNLOAD_ATTEMPTS = 3


def normalize_machine(machine: str) -> str:
    normalized = machine.strip().lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
    }
    return aliases.get(normalized, normalized)


def select_runtime(machine: str) -> RuntimeArtifact:
    normalized = normalize_machine(machine)
    try:
        return RUNTIME_ARTIFACTS[normalized]
    except KeyError as exc:
        raise RuntimeError(
            f"DSH runtime {DSH_RUNTIME_VERSION} does not support architecture "
            f"{machine!r}"
        ) from exc


def _runtime_member(archive: zipfile.ZipFile, artifact: RuntimeArtifact) -> str:
    expected = f"dsh-jsonrpc-agent-pkg-{artifact.binary_suffix}"
    matches = [
        name
        for name in archive.namelist()
        if not name.endswith("/") and Path(name).name == expected
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {expected!r} executable in {artifact.filename}, "
            f"found {len(matches)}"
        )
    return matches[0]


def _download_runtime(artifact: RuntimeArtifact, wheel: Path) -> None:
    request = urllib.request.Request(
        artifact.url,
        headers={"User-Agent": "moi-benchmark-dsh-adapter/1"},
    )
    for attempt in range(DOWNLOAD_ATTEMPTS):
        try:
            with urllib.request.urlopen(request, timeout=180) as response, wheel.open(
                "wb"
            ) as output:
                shutil.copyfileobj(response, output)
            return
        except (OSError, http.client.HTTPException):
            if attempt + 1 == DOWNLOAD_ATTEMPTS:
                raise
            time.sleep(2**attempt)


def _wheel_sha256(wheel: Path) -> str:
    return hashlib.sha256(wheel.read_bytes()).hexdigest()


def ensure_runtime_wheel(
    destination: Path, *, machine: Optional[str] = None
) -> RuntimeArtifact:
    artifact = select_runtime(machine or platform.machine())
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not destination.is_file():
        raise RuntimeError(f"DSH runtime wheel cache is not a file: {destination}")
    if destination.is_file() and _wheel_sha256(destination) == artifact.sha256:
        return artifact

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{artifact.filename}.", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        _download_runtime(artifact, temporary)
        actual_sha256 = _wheel_sha256(temporary)
        if actual_sha256 != artifact.sha256:
            raise RuntimeError(
                f"DSH runtime wheel SHA-256 mismatch: expected {artifact.sha256}, "
                f"got {actual_sha256}"
            )
        os.replace(temporary, destination)
        destination.chmod(0o444)
    finally:
        temporary.unlink(missing_ok=True)
    return artifact


def _extract_runtime(
    destination: Path, artifact: RuntimeArtifact, wheel: Path
) -> Path:
    actual_sha256 = _wheel_sha256(wheel)
    if actual_sha256 != artifact.sha256:
        raise RuntimeError(
            f"DSH runtime wheel SHA-256 mismatch: expected {artifact.sha256}, "
            f"got {actual_sha256}"
        )
    with zipfile.ZipFile(wheel) as archive:
        member = _runtime_member(archive, artifact)
        runtime_path = destination / "dsh-jsonrpc-agent"
        with archive.open(member) as source, runtime_path.open("wb") as output:
            shutil.copyfileobj(source, output)
    return runtime_path


def install_runtime(
    destination: Path,
    *,
    machine: Optional[str] = None,
    wheel_path: Optional[Path] = None,
) -> dict[str, object]:
    artifact = select_runtime(machine or platform.machine())
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(
            f"refusing to overwrite non-empty runtime directory {destination}"
        )
    destination.mkdir(parents=True, exist_ok=True)

    if wheel_path is not None:
        wheel = wheel_path.resolve()
        if not wheel.is_file():
            raise RuntimeError(f"DSH runtime wheel was not found: {wheel}")
        runtime_path = _extract_runtime(destination, artifact, wheel)
    else:
        with tempfile.TemporaryDirectory(prefix="dsh-runtime-download-") as directory:
            wheel = Path(directory) / artifact.filename
            _download_runtime(artifact, wheel)
            runtime_path = _extract_runtime(destination, artifact, wheel)

    runtime_path.chmod(0o555)
    marker: dict[str, object] = {
        "schema_version": 1,
        "product": "deepseek-harness",
        "version": DSH_RUNTIME_VERSION,
        "architecture": artifact.arch,
        "wheel_filename": artifact.filename,
        "wheel_sha256": artifact.sha256,
        "runtime_path": str(runtime_path),
    }
    marker_path = destination / "install.json"
    marker_path.write_text(
        json.dumps(marker, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    marker_path.chmod(0o444)
    return marker


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--wheel", type=Path)
    args = parser.parse_args()
    marker = install_runtime(args.destination, wheel_path=args.wheel)
    print(json.dumps(marker, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
