from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from harbor.models.task.config import TaskOS
from harbor.models.trial.paths import EnvironmentPaths
from harbor.verifier.verifier import Verifier

from astra.runners.pi_terminal_bench.verifier_evidence import (
    VerifierEvidenceError,
    validate_binary_reward,
    validate_ctrf_report,
)


class VerifierInfrastructureError(RuntimeError):
    pass


REMOTE_BOOTSTRAP_ROOT = "/tmp/moi-pi-verifier-bootstrap"
UV_COMMAND_RE = re.compile(r"^\s*(?:uv|uvx)(?:\s|\\|$)", re.MULTILINE)
UV_REQUIREMENT_RE = re.compile(r"(?:^|\s)-w\s+([^\s\\]+)")
OFFLINE_CTRF_REQUIREMENTS = frozenset(
    {
        "pytest==8.3.4",
        "pytest==8.4.1",
        "pytest==8.4.2",
        "pytest-json-ctrf==0.3.5",
    }
)
PYTHON_ARCHIVES = {
    "3.11": (
        "cpython-3.11.14+20251014-x86_64-unknown-linux-gnu-"
        "install_only_stripped.tar.gz"
    ),
    "3.12": (
        "cpython-3.12.12+20251014-x86_64-unknown-linux-gnu-"
        "install_only_stripped.tar.gz"
    ),
    "3.13": (
        "cpython-3.13.9+20251014-x86_64-unknown-linux-gnu-"
        "install_only_stripped.tar.gz"
    ),
}
PYTHON_VERSION_RE = re.compile(r"(?:^|\s)-p\s+(3\.(?:11|12|13))(?:\s|\\|$)")


class TerminalBenchEvidenceVerifier(Verifier):
    """Reject rewards produced before the Terminal-Bench tests ran."""

    def __init__(self, *args, cache_root: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._cache_root = cache_root or os.environ.get(
            "PI_TBENCH_VERIFIER_CACHE"
        )

    @staticmethod
    def _require_cache_file(path: Path) -> Path:
        if path.is_symlink() or not path.is_file():
            raise VerifierInfrastructureError(
                f"missing verifier bootstrap cache file: {path}"
            )
        return path

    async def _prepare_bootstrap_cache(self) -> None:
        cache_root = getattr(self, "_cache_root", None)
        if not cache_root:
            return

        test_path = self._resolve_tests()[2]
        try:
            test_script = test_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise VerifierInfrastructureError(
                f"could not read verifier test script: {test_path}"
            ) from exc
        if UV_COMMAND_RE.search(test_script) is None:
            return

        python_match = PYTHON_VERSION_RE.search(test_script)
        if python_match is None:
            raise VerifierInfrastructureError(
                f"could not determine uv Python version from {test_path}"
            )
        python_minor = python_match.group(1)
        try:
            root = Path(cache_root).expanduser().resolve()
        except OSError as exc:
            raise VerifierInfrastructureError(
                f"could not resolve verifier bootstrap cache: {cache_root}"
            ) from exc
        uv = self._require_cache_file(root / "bin" / "uv")
        uvx = self._require_cache_file(root / "bin" / "uvx")
        curl_wrapper = self._require_cache_file(root / "bin" / "curl")
        python_archive = self._require_cache_file(
            root
            / "python-build-standalone"
            / "20251014"
            / PYTHON_ARCHIVES[python_minor]
        )
        wheelhouse = root / "wheels"
        try:
            wheels = sorted(
                path
                for path in wheelhouse.iterdir()
                if path.suffix == ".whl" and not path.is_symlink()
            )
        except OSError as exc:
            raise VerifierInfrastructureError(
                f"missing verifier bootstrap wheelhouse: {wheelhouse}"
            ) from exc
        if not wheels:
            raise VerifierInfrastructureError(
                f"empty verifier bootstrap wheelhouse: {wheelhouse}"
            )

        remote_python_dir = f"{REMOTE_BOOTSTRAP_ROOT}/python-build-standalone/20251014"
        remote_wheelhouse = f"{REMOTE_BOOTSTRAP_ROOT}/wheels"
        setup = await self.environment.exec(
            command=(
                f"rm -rf -- {shlex.quote(REMOTE_BOOTSTRAP_ROOT)} && "
                "install -d -m 0755 "
                f"{shlex.quote(REMOTE_BOOTSTRAP_ROOT + '/bin')} "
                f"{shlex.quote(remote_python_dir)} "
                f"{shlex.quote(remote_wheelhouse)} /root/.local/bin && "
                "printf '%s' \"$PATH\""
            ),
            user="root",
        )
        if setup.return_code != 0:
            raise VerifierInfrastructureError(
                "failed to create verifier bootstrap cache directories: "
                f"{setup.stderr or setup.stdout or setup.return_code}"
            )

        try:
            await self.environment.upload_file(
                uv, f"{REMOTE_BOOTSTRAP_ROOT}/bin/uv"
            )
            await self.environment.upload_file(
                uvx, f"{REMOTE_BOOTSTRAP_ROOT}/bin/uvx"
            )
            await self.environment.upload_file(
                curl_wrapper, f"{REMOTE_BOOTSTRAP_ROOT}/bin/curl"
            )
            await self.environment.upload_file(
                python_archive, f"{remote_python_dir}/{python_archive.name}"
            )
            for wheel in wheels:
                await self.environment.upload_file(
                    wheel, f"{remote_wheelhouse}/{wheel.name}"
                )
        except Exception as exc:
            raise VerifierInfrastructureError(
                "failed to copy the verifier bootstrap cache into the task container"
            ) from exc

        executable_paths = [
            f"{REMOTE_BOOTSTRAP_ROOT}/bin/uv",
            f"{REMOTE_BOOTSTRAP_ROOT}/bin/uvx",
            f"{REMOTE_BOOTSTRAP_ROOT}/bin/curl",
        ]
        activate = await self.environment.exec(
            command=(
                "chmod 0755 "
                + " ".join(shlex.quote(path) for path in executable_paths)
                + " && printf '%s\\n' "
                + shlex.quote(
                    "export PATH=" + REMOTE_BOOTSTRAP_ROOT + "/bin:$PATH"
                )
                + " > /root/.local/bin/env"
            ),
            user="root",
        )
        if activate.return_code != 0:
            raise VerifierInfrastructureError(
                "failed to activate the verifier bootstrap cache: "
                f"{activate.stderr or activate.stdout or activate.return_code}"
            )

        install_python = await self.environment.exec(
            command=(
                "env "
                f"UV_CACHE_DIR={shlex.quote(REMOTE_BOOTSTRAP_ROOT + '/uv-cache')} "
                "UV_PYTHON_INSTALL_DIR="
                f"{shlex.quote(REMOTE_BOOTSTRAP_ROOT + '/python')} "
                "UV_PYTHON_INSTALL_MIRROR="
                f"{shlex.quote('file://' + REMOTE_BOOTSTRAP_ROOT + '/python-build-standalone')} "
                f"{shlex.quote(REMOTE_BOOTSTRAP_ROOT + '/bin/uv')} "
                f"python install {shlex.quote(python_minor)}"
            ),
            user="root",
        )
        if install_python.return_code != 0:
            raise VerifierInfrastructureError(
                "failed to install cached verifier Python: "
                f"{install_python.stderr or install_python.stdout or install_python.return_code}"
            )

        original_path = (setup.stdout or "").strip()
        if not original_path:
            raise VerifierInfrastructureError(
                "task container did not report its PATH during cache setup"
            )
        self.override_env.update(
            {
                "PATH": f"{REMOTE_BOOTSTRAP_ROOT}/bin:{original_path}",
                "UV_CACHE_DIR": f"{REMOTE_BOOTSTRAP_ROOT}/uv-cache",
                "UV_PYTHON_CACHE_DIR": (
                    f"{REMOTE_BOOTSTRAP_ROOT}/python-downloads"
                ),
                "UV_PYTHON_INSTALL_DIR": f"{REMOTE_BOOTSTRAP_ROOT}/python",
                "UV_PYTHON_INSTALL_MIRROR": (
                    f"file://{REMOTE_BOOTSTRAP_ROOT}/python-build-standalone"
                ),
                "UV_FIND_LINKS": f"file://{remote_wheelhouse}",
                "PIP_FIND_LINKS": remote_wheelhouse,
            }
        )
        uv_requirements = frozenset(UV_REQUIREMENT_RE.findall(test_script))
        if uv_requirements and uv_requirements <= OFFLINE_CTRF_REQUIREMENTS:
            self.override_env["UV_OFFLINE"] = "1"
        self.logger.info(
            "Prepared local uv 0.9.5 and CPython %s verifier bootstrap cache",
            python_minor,
        )

    async def _make_ctrf_readable(self) -> None:
        if self.environment.os == TaskOS.WINDOWS:
            return
        ctrf_path = str(
            EnvironmentPaths.for_os(self.environment.os).verifier_dir
            / "ctrf.json"
        )
        quoted_path = shlex.quote(ctrf_path)
        result = await self.environment.exec(
            command=(
                f"if [ -e {quoted_path} ]; then chmod 0644 {quoted_path}; fi"
            ),
            user="root",
        )
        if result.return_code != 0:
            raise VerifierInfrastructureError(
                "failed to make verifier ctrf.json readable: "
                f"{result.stderr or result.stdout or result.return_code}"
            )

    async def verify(self):
        await self._prepare_bootstrap_cache()
        result = await super().verify()
        await self._make_ctrf_readable()
        try:
            validate_ctrf_report(self.trial_paths.verifier_dir / "ctrf.json")
            validate_binary_reward(
                result.rewards.get("reward") if result.rewards else None
            )
        except VerifierEvidenceError as exc:
            raise VerifierInfrastructureError(str(exc)) from exc
        return result
