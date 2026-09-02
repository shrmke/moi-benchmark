from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, TextIO

from astra.runners.toolathlon_verified.adapter_common import strip_provider_credentials
from astra.runners.toolathlon_verified.contract import (
    ContractError,
    read_json_object,
    validate_loopback_url,
)
from astra.runners.toolathlon_verified.model_proxy import (
    ModelProxyConfig,
    provider_key_environment,
)


NODE_MODEL_PROXY = Path(__file__).with_name("node_model_proxy.mjs").resolve()
_START_TIMEOUT_SECONDS = 30.0
_STOP_TIMEOUT_SECONDS = 10.0


class _ExceededView:
    def __init__(self, budget: "_BudgetView") -> None:
        self._budget = budget

    def is_set(self) -> bool:
        return bool(self._budget.snapshot()["limit_exceeded"])


class _BudgetView:
    def __init__(self, proxy: "NodeModelProxyServer") -> None:
        self._proxy = proxy
        self.exceeded = _ExceededView(self)

    def snapshot(self) -> dict[str, Any]:
        self._proxy._raise_if_exited()
        state = read_json_object(self._proxy.config.state_path)
        budget = state.get("budget")
        if not isinstance(budget, dict):
            raise ContractError("Node model proxy state has no budget object")
        return dict(budget)


class NodeModelProxyServer:
    """Run the DSH model boundary on Node's built-in Undici fetch stack."""

    def __init__(self, config: ModelProxyConfig, *, node: Path) -> None:
        self.config = config
        self.node = node.resolve()
        self.budget = _BudgetView(self)
        self.process: subprocess.Popen[str] | None = None
        self.url = ""
        self.undici_version = ""
        self._closing = False
        self._messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._readers: list[threading.Thread] = []

    def _read_lines(self, name: str, stream: TextIO) -> None:
        try:
            for line in stream:
                value = line.rstrip("\r\n")
                if name == "stderr" and value:
                    self._stderr_tail.append(value)
                self._messages.put((name, value))
        finally:
            stream.close()

    def _error_detail(self) -> str:
        return "\n".join(self._stderr_tail) or "no stderr was emitted"

    def _raise_if_exited(self) -> None:
        process = self.process
        if process is None:
            raise ContractError("Node model proxy is not started")
        return_code = process.poll()
        if return_code is not None and not self._closing:
            raise ContractError(
                f"Node model proxy exited with code {return_code}: {self._error_detail()}"
            )

    def start(self) -> None:
        if self.process is not None:
            raise ContractError("Node model proxy already started")
        if not NODE_MODEL_PROXY.is_file() or NODE_MODEL_PROXY.is_symlink():
            raise ContractError(f"Node model proxy script is unavailable: {NODE_MODEL_PROXY}")
        if not self.node.is_file():
            raise ContractError(f"Node executable is unavailable: {self.node}")

        credential_environment = provider_key_environment(self.config.system_id)
        payload = {
            "listen_host": "127.0.0.1",
            "listen_port": 0,
            "upstream_base_url": self.config.upstream_base_url,
            "upstream_api_key": self.config.upstream_api_key,
            "effective_model": self.config.effective_model,
            "temperature": self.config.temperature,
            "thinking": self.config.thinking,
            "reasoning_effort": self.config.reasoning_effort,
            "max_requests": self.config.max_requests,
            "run_id": self.config.run_id,
            "system_id": self.config.system_id,
            "events_path": str(self.config.events_path),
            "state_path": str(self.config.state_path),
            "provider_credential_environment": credential_environment,
            "provider_credential_fingerprint": (
                self.config.provider_credential_fingerprint
            ),
            "provider_user_id": self.config.provider_user_id,
        }
        environment = dict(os.environ)
        strip_provider_credentials(environment)
        environment.pop(credential_environment, None)
        process = subprocess.Popen(
            [str(self.node), str(NODE_MODEL_PROXY)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            close_fds=True,
            env=environment,
        )
        self.process = process
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        self._readers = [
            threading.Thread(
                target=self._read_lines,
                args=(name, stream),
                name=f"dsh-model-proxy-{name}",
                daemon=True,
            )
            for name, stream in (
                ("stdout", process.stdout),
                ("stderr", process.stderr),
            )
        ]
        for reader in self._readers:
            reader.start()
        try:
            process.stdin.write(json.dumps(payload, separators=(",", ":")))
            process.stdin.close()
            deadline = time.monotonic() + _START_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                self._raise_if_exited()
                try:
                    source, line = self._messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                if source != "stdout" or not line:
                    continue
                try:
                    ready = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ContractError(
                        f"Node model proxy emitted invalid startup output: {line!r}"
                    ) from exc
                if not isinstance(ready, dict) or ready.get("event") != "ready":
                    raise ContractError(
                        f"Node model proxy emitted unexpected startup output: {line!r}"
                    )
                self.url = validate_loopback_url(
                    "Node model proxy URL", str(ready.get("url", ""))
                )
                self.undici_version = str(ready.get("undici_version", ""))
                self.budget.snapshot()
                return
            raise ContractError("Node model proxy did not become ready before timeout")
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        self._closing = True
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=_STOP_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for reader in self._readers:
                reader.join(timeout=1)
        finally:
            self.process = None
            self._closing = False

    def __enter__(self) -> "NodeModelProxyServer":
        self.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()
