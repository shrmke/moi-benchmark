"""Freeze Hermes C0 process policy before Hermes imports application code."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


_STATIC_PINS = {
    "HERMES_HOME": "/tmp/hermes",
    "HERMES_MANAGED_DIR": "/etc/hermes",
    "HERMES_YOLO_MODE": "0",
    "HERMES_ACCEPT_HOOKS": "0",
    "HERMES_EXEC_ASK": "1",
}
_DYNAMIC_PIN_NAMES = (
    "API_SERVER_HOST",
    "API_SERVER_KEY",
    "API_SERVER_PORT",
    "DEEPSEEK_API_KEY",
    "GLM_API_KEY",
    "ZAI_API_KEY",
    "Z_AI_API_KEY",
)
_PINS = {
    **_STATIC_PINS,
    **{
        name: os.environ[name]
        for name in _DYNAMIC_PIN_NAMES
        if os.environ.get(name)
    },
}
_ORIGINAL_SETITEM = os._Environ.__setitem__
_ORIGINAL_DELITEM = os._Environ.__delitem__
_ORIGINAL_PUTENV = os.putenv
_ORIGINAL_UNSETENV = os.unsetenv


def _key_text(key: Any) -> str:
    if isinstance(key, bytes):
        return key.decode(sys.getfilesystemencoding(), "surrogateescape")
    return str(key)


def _pin_value(key: Any, value: str) -> str | bytes:
    if isinstance(key, bytes):
        return os.fsencode(value)
    return value


def _guarded_setitem(
    environment: os._Environ[Any],
    key: Any,
    value: Any,
) -> None:
    pinned = _PINS.get(_key_text(key))
    _ORIGINAL_SETITEM(
        environment,
        key,
        _pin_value(key, pinned) if pinned is not None else value,
    )


def _guarded_delitem(environment: os._Environ[Any], key: Any) -> None:
    pinned = _PINS.get(_key_text(key))
    if pinned is not None:
        _ORIGINAL_SETITEM(environment, key, _pin_value(key, pinned))
        return
    _ORIGINAL_DELITEM(environment, key)


def _guarded_putenv(key: Any, value: Any) -> None:
    pinned = _PINS.get(_key_text(key))
    _ORIGINAL_PUTENV(
        key,
        _pin_value(key, pinned) if pinned is not None else value,
    )


def _guarded_unsetenv(key: Any) -> None:
    pinned = _PINS.get(_key_text(key))
    if pinned is not None:
        _ORIGINAL_PUTENV(key, _pin_value(key, pinned))
        return
    _ORIGINAL_UNSETENV(key)


def _fail_closed(message: str) -> None:
    os.write(2, f"Hermes C0 policy guard: {message}\n".encode())
    os._exit(126)


source_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
expected_sha256 = os.environ.get("HERMES_C0_POLICY_GUARD_SHA256", "")
if not expected_sha256 or source_sha256 != expected_sha256:
    _fail_closed("source digest mismatch")

os._Environ.__setitem__ = _guarded_setitem
os._Environ.__delitem__ = _guarded_delitem
os.putenv = _guarded_putenv
os.unsetenv = _guarded_unsetenv
for pin_name, pin_value in _PINS.items():
    _ORIGINAL_SETITEM(os.environ, pin_name, pin_value)

evidence_path = os.environ.get("HERMES_C0_POLICY_GUARD_EVIDENCE", "")
if not evidence_path:
    _fail_closed("evidence path is missing")
try:
    evidence = Path(evidence_path)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    with evidence.open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {
                    "dynamic_pin_names": sorted(
                        name for name in _DYNAMIC_PIN_NAMES if name in _PINS
                    ),
                    "event": "policy_guard.loaded",
                    "pid": os.getpid(),
                    "source_sha256": source_sha256,
                    "static_pins": _STATIC_PINS,
                    "timestamp": time.time(),
                },
                sort_keys=True,
            )
        )
        stream.write("\n")
except OSError as exc:
    _fail_closed(f"could not persist evidence: {exc}")
