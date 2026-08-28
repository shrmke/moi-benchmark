"""A small, dependency-free load benchmark for RAG product APIs.

The benchmark deliberately measures the transport contract rather than trying to
make four products expose the same internal implementation.  Streaming
endpoints are counted as SSE events, while a non-streaming JSON response is
counted as one response event.  This keeps the result explicit when a product
does not support a requested protocol.
"""

from __future__ import annotations

import argparse
import codecs
import json
import os
import re
import statistics
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from threading import Lock
from typing import Any, Iterable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


BENCHMARK_VERSION = "0.3"
USER_AGENT = f"MOI-RAG-Benchmark/{BENCHMARK_VERSION}"
SUPPORTED_PROTOCOLS = {"auto", "sse", "json"}
SCENARIO_NAMES = {"events", "empty_workflow"}


def _load_dotenv_file(path: Path, environ: dict[str, str]) -> None:
    """Load simple KEY=VALUE entries without replacing explicit environment values."""

    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not key or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        environ.setdefault(key, value)


def _load_benchmark_dotenv(environ: dict[str, str]) -> None:
    """Load only the repository-root .env for both entry points."""

    root_env = Path(__file__).resolve().parents[3] / ".env"
    _load_dotenv_file(root_env, environ)


class BenchmarkConfigError(ValueError):
    """Raised when a benchmark target or scenario is not runnable."""


@dataclass(frozen=True)
class StreamEvent:
    """One logical event decoded from a Server-Sent Events stream."""

    event_type: str
    data: str
    json_data: Any | None = None


class _SSEParser:
    """Incremental SSE parser that preserves event boundaries across chunks."""

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buffer = ""
        self._event_name = ""
        self._data_lines: list[str] = []

    def feed(self, chunk: bytes | str) -> list[StreamEvent]:
        if isinstance(chunk, bytes):
            self._buffer += self._decoder.decode(chunk, final=False)
        else:
            self._buffer += chunk
        events: list[StreamEvent] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            events.extend(self._consume_line(line.rstrip("\r")))
        return events

    def finish(self) -> list[StreamEvent]:
        self._buffer += self._decoder.decode(b"", final=True)
        events: list[StreamEvent] = []
        if self._buffer:
            events.extend(self._consume_line(self._buffer.rstrip("\r")))
            self._buffer = ""
        events.extend(self._flush_event())
        return events

    def _consume_line(self, line: str) -> list[StreamEvent]:
        if line == "":
            return self._flush_event()
        if line.startswith(":"):
            return []
        if line.startswith("event:"):
            self._event_name = line[6:].lstrip(" ")
            return []
        if line.startswith("data:"):
            self._data_lines.append(line[5:].lstrip(" "))
        return []

    def _flush_event(self) -> list[StreamEvent]:
        if not self._data_lines:
            self._event_name = ""
            return []
        data = "\n".join(self._data_lines)
        event_name = self._event_name
        self._event_name = ""
        self._data_lines = []
        parsed: Any | None = None
        try:
            parsed = json.loads(data)
        except (TypeError, ValueError):
            pass
        if event_name:
            event_type = event_name
        elif isinstance(parsed, dict):
            event_type = "message"
            if parsed.get("error") not in (None, "", False):
                event_type = "error"
            else:
                for key in ("event", "type", "step_type", "step_name", "source"):
                    value = parsed.get(key)
                    if value not in (None, ""):
                        event_type = str(value)
                        break
        else:
            event_type = "message"
        return [StreamEvent(event_type=event_type, data=data, json_data=parsed)]


def iter_sse_events(chunks: Iterable[bytes | str]) -> Iterator[StreamEvent]:
    """Yield logical SSE events from arbitrary byte/string chunks."""

    parser = _SSEParser()
    for chunk in chunks:
        yield from parser.feed(chunk)
    yield from parser.finish()


@dataclass(frozen=True)
class ScenarioConfig:
    """One HTTP operation used by a benchmark scenario."""

    name: str
    path: str
    method: str = "POST"
    body: Any | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    protocol: str = "auto"
    supported: bool = True
    note: str | None = None

    def __post_init__(self) -> None:
        if self.protocol not in SUPPORTED_PROTOCOLS:
            raise BenchmarkConfigError(
                f"unsupported response protocol {self.protocol!r}; "
                f"choose one of {sorted(SUPPORTED_PROTOCOLS)}"
            )
        if self.supported and not self.path:
            raise BenchmarkConfigError(f"supported scenario {self.name!r} needs a path")


@dataclass(frozen=True)
class TargetConfig:
    """Connection and authentication settings for one product."""

    name: str
    base_url: str
    api_key_env: str | None
    auth_header: str | None
    auth_scheme: str = "bearer"
    event: ScenarioConfig = field(
        default_factory=lambda: ScenarioConfig("events", "/")
    )
    empty_workflow: ScenarioConfig = field(
        default_factory=lambda: ScenarioConfig("empty_workflow", "/")
    )
    required_env: tuple[str, ...] = ()
    fallback_api_key_envs: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def scenario(self, name: str) -> ScenarioConfig:
        if name == "events":
            return self.event
        if name == "empty_workflow":
            return self.empty_workflow
        raise BenchmarkConfigError(f"unknown benchmark scenario: {name}")

    def resolve_api_key(self, environ: Mapping[str, str]) -> tuple[str | None, str | None]:
        names = []
        if self.api_key_env:
            names.append(self.api_key_env)
        names.extend(self.fallback_api_key_envs)
        for name in names:
            value = environ.get(name)
            if value:
                return value, name
        return None, None

    def missing_requirements(self, environ: Mapping[str, str]) -> list[str]:
        missing: list[str] = []
        if self.api_key_env and self.resolve_api_key(environ)[0] is None:
            names = [self.api_key_env, *self.fallback_api_key_envs]
            missing.append(" or ".join(names))
        for name in self.required_env:
            if not environ.get(name):
                missing.append(name)
        return missing


def _parse_json_env(
    environ: Mapping[str, str], name: str, default: Any
) -> Any:
    raw = environ.get(name)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BenchmarkConfigError(f"{name} must contain valid JSON: {exc}") from exc


def build_builtin_targets(environ: Mapping[str, str] | None = None) -> dict[str, TargetConfig]:
    """Build default profiles for MOI, Dify, FastGPT and MaxKB.

    The profiles intentionally only contain public endpoint shapes and read all
    credentials/app identifiers from environment variables.  A custom JSON
    config can override any profile when a deployment uses a different route.
    """

    env = dict(os.environ if environ is None else environ)
    question = env.get("RAG_BENCHMARK_QUESTION", "benchmark query")

    dify_inputs = _parse_json_env(env, "DIFY_BENCHMARK_INPUTS_JSON", {})
    dify_key_env = env.get("DIFY_BENCHMARK_API_KEY_ENV", "DIFY_API_KEY")
    dify_base = (
        env.get("DIFY_BENCHMARK_BASE_URL")
        or env.get("DIFY_API_BASE_URL")
        or "http://127.0.0.1:8010/v1"
    )
    dify_event_path = env.get("DIFY_BENCHMARK_EVENT_PATH", "/workflows/run")
    dify_event = ScenarioConfig(
        name="events",
        path=dify_event_path,
        protocol="sse",
        body={
            "inputs": dify_inputs,
            "response_mode": "streaming",
            "user": "{{uuid}}",
        },
    )
    dify_empty = ScenarioConfig(
        name="empty_workflow",
        path=env.get("DIFY_BENCHMARK_EMPTY_PATH", dify_event_path),
        protocol="json",
        body={
            "inputs": _parse_json_env(env, "DIFY_BENCHMARK_EMPTY_INPUTS_JSON", {}),
            "response_mode": "blocking",
            "user": "{{uuid}}",
        },
        note="Point the Dify API key at a no-op workflow for a true empty-workflow measurement.",
    )

    fastgpt_app_env = env.get("FASTGPT_BENCHMARK_APP_ID_ENV", "FASTGPT_APP_ID")
    fastgpt_key_env = env.get("FASTGPT_BENCHMARK_API_KEY_ENV", "FASTGPT_API_KEY")
    fastgpt_base = (
        env.get("FASTGPT_BENCHMARK_BASE_URL")
        or env.get("FASTGPT_BASE_URL")
        or "http://127.0.0.1:3000"
    )
    fastgpt_path = env.get("FASTGPT_BENCHMARK_PATH", "/api/v1/chat/completions")
    fastgpt_app_id = f"${{{fastgpt_app_env}}}"
    fastgpt_event = ScenarioConfig(
        name="events",
        path=fastgpt_path,
        protocol="sse",
        body={
            "appId": fastgpt_app_id,
            "chatId": "{{uuid}}",
            "stream": True,
            "detail": True,
            "messages": [{"role": "user", "content": question}],
        },
    )
    fastgpt_empty = ScenarioConfig(
        name="empty_workflow",
        path=fastgpt_path,
        protocol="json",
        body={
            "appId": fastgpt_app_id,
            "chatId": "{{uuid}}",
            "stream": False,
            "detail": False,
            "messages": [{"role": "user", "content": question}],
        },
        note="Point FASTGPT_APP_ID at a no-op app for a true empty-workflow measurement.",
    )

    maxkb_app_env = env.get(
        "MAXKB_BENCHMARK_APPLICATION_ID_ENV", "MAXKB_APPLICATION_ID"
    )
    maxkb_key_env = env.get("MAXKB_BENCHMARK_API_KEY_ENV", "MAXKB_API_KEY")
    maxkb_base = (
        env.get("MAXKB_BENCHMARK_BASE_URL")
        or env.get("MAXKB_OPENAI_BASE_URL")
        or env.get("MAXKB_BASE_URL")
        or "http://127.0.0.1:8090"
    )
    maxkb_app_id = f"${{{maxkb_app_env}}}"
    maxkb_path = env.get("MAXKB_BENCHMARK_PATH") or env.get(
        "MAXKB_OPENAI_PATH", f"/chat/api/{maxkb_app_id}/chat/completions"
    )
    maxkb_event = ScenarioConfig(
        name="events",
        path=maxkb_path,
        protocol="sse",
        headers={"Accept": "*/*"},
        body={
            "model": env.get("MAXKB_MODEL", "maxkb"),
            "messages": [{"role": "user", "content": question}],
            "stream": True,
        },
    )
    maxkb_empty = ScenarioConfig(
        name="empty_workflow",
        path=maxkb_path,
        protocol="json",
        body={
            "model": env.get("MAXKB_MODEL", "maxkb"),
            "messages": [{"role": "user", "content": question}],
            "stream": False,
        },
        note="Point MAXKB_APPLICATION_ID at a no-op application for a true empty-workflow measurement.",
    )

    moi_key_env = env.get("MOI_BENCHMARK_API_KEY_ENV", "MOI_API_KEY")
    moi_base = (
        env.get("MOI_BENCHMARK_BASE_URL")
        or env.get("MOI_API_URL")
        or "http://127.0.0.1:8000"
    )
    moi_event_path = env.get(
        "MOI_BENCHMARK_EVENT_PATH", "/byoa/api/v1/data_asking/analyze"
    )
    moi_event = ScenarioConfig(
        name="events",
        path=moi_event_path,
        protocol="sse",
        body={
            "question": question,
            "session_id": "{{uuid}}",
            "config": {
                "data_category": env.get("MOI_DATA_CATEGORY", "admin"),
                "data_source": {"type": env.get("MOI_DATA_SOURCE_TYPE", "all")},
            },
        },
        note="MOI Data Asking SSE: init/classification/step/chunk/complete events are counted.",
    )
    moi_empty_path = env.get("MOI_BENCHMARK_EMPTY_PATH")
    if moi_empty_path:
        moi_empty = ScenarioConfig(
            name="empty_workflow",
            path=moi_empty_path,
            protocol=env.get("MOI_BENCHMARK_EMPTY_PROTOCOL", "json"),
            body=_parse_json_env(
                env,
                "MOI_BENCHMARK_EMPTY_BODY_JSON",
                {"question": "benchmark noop", "session_id": "{{uuid}}"},
            ),
            note="Custom MOI empty-workflow endpoint supplied by the deployment.",
        )
    else:
        moi_empty = ScenarioConfig(
            name="empty_workflow",
            path="",
            protocol="json",
            supported=False,
            note=(
                "MOI Data Asking is a RAG analysis API, not a universally stable "
                "empty-workflow API. Set MOI_BENCHMARK_EMPTY_PATH or use a JSON config."
            ),
        )

    return {
        "moi": TargetConfig(
            name="moi",
            base_url=moi_base,
            api_key_env=moi_key_env,
            auth_header="moi-key",
            auth_scheme="raw",
            event=moi_event,
            empty_workflow=moi_empty,
            metadata={"family": "MatrixOne Intelligence", "event_contract": "SSE"},
        ),
        "dify": TargetConfig(
            name="dify",
            base_url=dify_base,
            api_key_env=dify_key_env,
            auth_header="Authorization",
            auth_scheme="bearer",
            event=dify_event,
            empty_workflow=dify_empty,
            metadata={"family": "Dify", "event_contract": "workflow SSE"},
        ),
        "fastgpt": TargetConfig(
            name="fastgpt",
            base_url=fastgpt_base,
            api_key_env=fastgpt_key_env,
            auth_header="Authorization",
            auth_scheme="bearer",
            event=fastgpt_event,
            empty_workflow=fastgpt_empty,
            required_env=(fastgpt_app_env,),
            metadata={"family": "FastGPT", "event_contract": "OpenAI-compatible SSE"},
        ),
        "maxkb": TargetConfig(
            name="maxkb",
            base_url=maxkb_base,
            api_key_env=maxkb_key_env,
            auth_header="Authorization",
            auth_scheme="bearer",
            event=maxkb_event,
            empty_workflow=maxkb_empty,
            required_env=(maxkb_app_env,),
            metadata={"family": "MaxKB", "event_contract": "OpenAI-compatible SSE"},
        ),
    }


def _scenario_from_mapping(
    name: str,
    raw: Mapping[str, Any] | None,
    fallback: ScenarioConfig | None,
) -> ScenarioConfig:
    if raw is None:
        if fallback is None:
            return ScenarioConfig(name=name, path="", supported=False, note="not configured")
        return fallback
    if not isinstance(raw, Mapping):
        raise BenchmarkConfigError(f"scenario {name!r} must be an object")
    supported = bool(raw.get("supported", fallback.supported if fallback else True))
    path = str(raw.get("path", fallback.path if fallback else ""))
    protocol = str(raw.get("protocol", fallback.protocol if fallback else "auto"))
    body = raw.get("body", fallback.body if fallback else None)
    headers = dict(fallback.headers if fallback else {})
    raw_headers = raw.get("headers", {})
    if not isinstance(raw_headers, Mapping):
        raise BenchmarkConfigError(f"scenario {name!r}.headers must be an object")
    headers.update({str(key): str(value) for key, value in raw_headers.items()})
    note = raw.get("note", fallback.note if fallback else None)
    return ScenarioConfig(
        name=name,
        path=path,
        method=str(raw.get("method", fallback.method if fallback else "POST")),
        body=body,
        headers=headers,
        protocol=protocol,
        supported=supported,
        note=str(note) if note is not None else None,
    )


def load_benchmark_targets(
    config_path: str | Path | None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, TargetConfig]:
    """Load built-ins and apply optional target overrides from JSON."""

    env = dict(os.environ if environ is None else environ)
    targets = build_builtin_targets(env)
    if config_path is None:
        return targets
    path = Path(config_path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkConfigError(f"cannot read benchmark config {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkConfigError(f"invalid benchmark config {path}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise BenchmarkConfigError("benchmark config root must be an object")
    raw_targets = raw.get("targets", raw)
    if not isinstance(raw_targets, Mapping):
        raise BenchmarkConfigError("benchmark config 'targets' must be an object")

    for name, value in raw_targets.items():
        if not isinstance(value, Mapping):
            raise BenchmarkConfigError(f"target {name!r} must be an object")
        target_name = str(name)
        fallback = targets.get(target_name)
        base_url = str(value.get("base_url", fallback.base_url if fallback else ""))
        if not base_url:
            raise BenchmarkConfigError(f"target {target_name!r} needs base_url")
        auth_raw = value.get("auth", {})
        if auth_raw is None:
            auth_raw = {}
        if not isinstance(auth_raw, Mapping):
            raise BenchmarkConfigError(f"target {target_name!r}.auth must be an object")
        api_key_env = value.get(
            "api_key_env", fallback.api_key_env if fallback else None
        )
        auth_header = value.get(
            "auth_header",
            auth_raw.get("header", fallback.auth_header if fallback else None),
        )
        auth_scheme = str(
            value.get(
                "auth_scheme",
                auth_raw.get("scheme", fallback.auth_scheme if fallback else "bearer"),
            )
        )
        required_raw = value.get(
            "required_env", fallback.required_env if fallback else ()
        )
        if isinstance(required_raw, str):
            required_env = (required_raw,)
        elif isinstance(required_raw, Sequence):
            required_env = tuple(str(item) for item in required_raw)
        else:
            raise BenchmarkConfigError(f"target {target_name!r}.required_env must be a list")
        fallback_keys = value.get(
            "fallback_api_key_envs", fallback.fallback_api_key_envs if fallback else ()
        )
        if isinstance(fallback_keys, str):
            fallback_api_key_envs = (fallback_keys,)
        else:
            fallback_api_key_envs = tuple(str(item) for item in (fallback_keys or ()))
        metadata = dict(fallback.metadata if fallback else {})
        raw_metadata = value.get("metadata", {})
        if not isinstance(raw_metadata, Mapping):
            raise BenchmarkConfigError(f"target {target_name!r}.metadata must be an object")
        metadata.update(raw_metadata)
        event = _scenario_from_mapping(
            "events", value.get("event", value.get("events")), fallback.event if fallback else None
        )
        empty = _scenario_from_mapping(
            "empty_workflow", value.get("empty_workflow"), fallback.empty_workflow if fallback else None
        )
        targets[target_name] = TargetConfig(
            name=target_name,
            base_url=base_url,
            api_key_env=str(api_key_env) if api_key_env is not None else None,
            auth_header=str(auth_header) if auth_header is not None else None,
            auth_scheme=auth_scheme,
            event=event,
            empty_workflow=empty,
            required_env=required_env,
            fallback_api_key_envs=fallback_api_key_envs,
            metadata=metadata,
        )
    return targets


_TEMPLATE_RE = re.compile(r"\{\{(uuid|timestamp|env:[A-Za-z_][A-Za-z0-9_]*)\}\}|\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def render_template(value: Any, environ: Mapping[str, str], request_id: str) -> Any:
    """Render safe request placeholders recursively.

    Supported placeholders are ``{{uuid}}``, ``{{timestamp}}``,
    ``{{env:NAME}}`` and ``${NAME}``.  No credentials are inserted unless a
    user explicitly places one in a request body template.
    """

    if isinstance(value, Mapping):
        return {key: render_template(item, environ, request_id) for key, item in value.items()}
    if isinstance(value, list):
        return [render_template(item, environ, request_id) for item in value]
    if isinstance(value, tuple):
        return [render_template(item, environ, request_id) for item in value]
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        first, env_name = match.groups()
        if first == "uuid":
            return request_id
        if first == "timestamp":
            return str(time.time_ns())
        if first and first.startswith("env:"):
            return environ.get(first[4:], "")
        return environ.get(env_name or "", "")

    return _TEMPLATE_RE.sub(replace, value)


def _join_request_url(base_url: str, path: str) -> tuple[str, str, int | None, str]:
    if "://" in path:
        parsed = urlsplit(path)
    else:
        base = urlsplit(base_url)
        relative = urlsplit(path or "/")
        base_path = base.path.rstrip("/")
        relative_path = relative.path or "/"
        joined_path = f"{base_path}/{relative_path.lstrip('/')}" or "/"
        query = relative.query or base.query
        parsed = base._replace(path=joined_path, query=query)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BenchmarkConfigError(f"unsupported base URL: {base_url!r}")
    request_target = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    return parsed.scheme, parsed.hostname, parsed.port, request_target


def _read_response_chunk(response: Any, size: int) -> bytes:
    read1 = getattr(response, "read1", None)
    if callable(read1):
        return read1(size)
    return response.read(size)


def _is_error_event(event_type: str) -> bool:
    normalized = re.sub(r"[\s-]+", "_", str(event_type).strip().casefold())
    return normalized in {"error", "failed", "failure"} or normalized.endswith(
        ("_error", "_failed", "_failure")
    )


def _count_error_events(event_type_counts: Mapping[str, Any]) -> int:
    return sum(
        int(count or 0)
        for event_type, count in event_type_counts.items()
        if _is_error_event(event_type)
    )


def _is_application_error_event(event: StreamEvent) -> bool:
    if _is_error_event(event.event_type):
        return True
    payload = event.json_data
    if not isinstance(payload, Mapping):
        return False
    if payload.get("error") not in (None, "", False):
        return True
    result = payload.get("result")
    if isinstance(result, Mapping):
        status = result.get("status")
        state = str(status.get("state") or "").strip().casefold() if isinstance(status, Mapping) else ""
        if result.get("final") is True and state in {"failed", "canceled", "cancelled", "rejected"}:
            return True
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return False
    status = str(data.get("status") or "").strip().casefold()
    return status in {"error", "failed", "failure"} or data.get("error") not in (
        None,
        "",
        False,
    )


def _terminal_event_state(event: StreamEvent) -> str | None:
    """Return success/failure for the four products' native SSE terminators."""

    if event.data.strip() == "[DONE]":
        return "success"
    event_type = str(event.event_type).strip().casefold().replace("-", "_")
    payload = event.json_data
    if event_type in {"complete", "message_end", "workflowduration"}:
        return "success"
    if event_type == "workflow_finished" and isinstance(payload, Mapping):
        data = payload.get("data")
        status = str(data.get("status") or "").strip().casefold() if isinstance(data, Mapping) else ""
        return "success" if status in {"succeeded", "success", "completed"} else "failure"
    if isinstance(payload, Mapping):
        choices = payload.get("choices")
        if isinstance(choices, list) and any(
            isinstance(choice, Mapping) and choice.get("finish_reason") not in (None, "")
            for choice in choices
        ):
            return "success"
        result = payload.get("result")
        if isinstance(result, Mapping) and result.get("kind") == "status-update" and result.get("final") is True:
            status = result.get("status")
            state = str(status.get("state") or "").strip().casefold() if isinstance(status, Mapping) else ""
            return "success" if state == "completed" else "failure"
    return None


class _InFlight:
    def __init__(self) -> None:
        self._lock = Lock()
        self.current = 0
        self.peak = 0
        self._last_change = time.perf_counter()
        self._area = 0.0

    def _advance(self, now: float) -> None:
        self._area += self.current * (now - self._last_change)
        self._last_change = now

    def enter(self) -> None:
        with self._lock:
            self._advance(time.perf_counter())
            self.current += 1
            self.peak = max(self.peak, self.current)

    def leave(self) -> None:
        with self._lock:
            self._advance(time.perf_counter())
            self.current = max(0, self.current - 1)

    def average(self, elapsed_s: float) -> float:
        with self._lock:
            self._advance(time.perf_counter())
            return self._area / max(elapsed_s, 0.000001)


def measure_request(
    target: TargetConfig,
    scenario: ScenarioConfig,
    environ: Mapping[str, str],
    timeout_s: float,
    worker_id: int = 0,
    request_index: int = 0,
) -> dict[str, Any]:
    """Execute and measure one request without logging credentials."""

    request_id = uuid.uuid4().hex
    started_ns = time.perf_counter_ns()
    status_code: int | None = None
    first_byte_ms: float | None = None
    first_event_ms: float | None = None
    bytes_received = 0
    events = 0
    event_type_counts: Counter[str] = Counter()
    application_error_events = 0
    terminal_success_events = 0
    terminal_failure_events = 0
    error: str | None = None
    connection: HTTPConnection | HTTPSConnection | None = None
    protocol = scenario.protocol
    try:
        api_key, _ = target.resolve_api_key(environ)
        scheme, host, port, request_target = _join_request_url(
            target.base_url, render_template(scenario.path, environ, request_id)
        )
        connection_cls = HTTPSConnection if scheme == "https" else HTTPConnection
        connection = connection_cls(host, port=port, timeout=timeout_s)
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": "text/event-stream" if protocol == "sse" else "*/*",
        }
        headers.update(
            {
                str(key): str(render_template(value, environ, request_id))
                for key, value in scenario.headers.items()
            }
        )
        body_value = render_template(scenario.body, environ, request_id)
        body: bytes | None
        if body_value is None:
            body = None
        elif isinstance(body_value, bytes):
            body = body_value
        elif isinstance(body_value, str):
            body = body_value.encode("utf-8")
        else:
            body = json.dumps(body_value, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        if body is not None and not any(key.lower() == "content-type" for key in headers):
            headers["Content-Type"] = "application/json"
        if target.auth_header and api_key and not any(
            key.lower() == target.auth_header.lower() for key in headers
        ):
            if target.auth_scheme.lower() in {"bearer", "token"}:
                headers[target.auth_header] = f"Bearer {api_key}"
            else:
                headers[target.auth_header] = api_key

        connection.request(scenario.method.upper(), request_target, body=body, headers=headers)
        response = connection.getresponse()
        status_code = int(response.status)
        content_type = response.getheader("Content-Type", "") or ""
        if protocol == "auto":
            protocol = "sse" if "text/event-stream" in content_type.lower() else "json"

        if not 200 <= status_code < 300:
            error_body = _read_response_chunk(response, 4096)
            bytes_received += len(error_body)
            error = f"http_status={status_code}"
        elif protocol == "sse":
            parser = _SSEParser()
            while True:
                chunk = _read_response_chunk(response, 4096)
                if not chunk:
                    break
                bytes_received += len(chunk)
                if first_byte_ms is None:
                    first_byte_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
                for event in parser.feed(chunk):
                    events += 1
                    event_type_counts[event.event_type] += 1
                    application_error_events += int(_is_application_error_event(event))
                    terminal_state = _terminal_event_state(event)
                    terminal_success_events += int(terminal_state == "success")
                    terminal_failure_events += int(terminal_state == "failure")
                    if first_event_ms is None:
                        first_event_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            for event in parser.finish():
                events += 1
                event_type_counts[event.event_type] += 1
                application_error_events += int(_is_application_error_event(event))
                terminal_state = _terminal_event_state(event)
                terminal_success_events += int(terminal_state == "success")
                terminal_failure_events += int(terminal_state == "failure")
                if first_event_ms is None:
                    first_event_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
        else:
            while True:
                chunk = _read_response_chunk(response, 4096)
                if not chunk:
                    break
                bytes_received += len(chunk)
                if first_byte_ms is None:
                    first_byte_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
                    events = 1
                    event_type_counts["response"] = 1
    except Exception as exc:  # transport errors must become samples, not abort the run
        error = f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        if connection is not None:
            connection.close()

    total_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
    transport_success = (
        error is None and status_code is not None and 200 <= status_code < 300
    )
    application_error = None
    if transport_success and protocol == "sse":
        if events == 0:
            application_error = "empty_sse_stream"
        elif terminal_failure_events:
            application_error = "terminal_failure"
        elif terminal_success_events == 0:
            application_error = "incomplete_sse_stream"
    return {
        "request_id": request_id,
        "worker_id": worker_id,
        "request_index": request_index,
        "status_code": status_code,
        "success": (
            transport_success
            and application_error_events == 0
            and application_error is None
        ),
        "transport_success": transport_success,
        "application_error_events": application_error_events,
        "application_error": application_error,
        "terminal_success_events": terminal_success_events,
        "terminal_failure_events": terminal_failure_events,
        "protocol": protocol,
        "events": events,
        "event_type_counts": dict(event_type_counts),
        "first_byte_ms": round(first_byte_ms, 3) if first_byte_ms is not None else None,
        "first_event_ms": round(first_event_ms, 3) if first_event_ms is not None else None,
        "total_ms": round(total_ms, 3),
        "stream_ms": round(total_ms, 3),
        "bytes_received": bytes_received,
        "error": error,
    }


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 3)


def _distribution(values: Iterable[float]) -> dict[str, float | int | None]:
    values_list = [float(value) for value in values]
    if not values_list:
        return {
            "count": 0,
            "min": None,
            "avg": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "max": None,
        }
    return {
        "count": len(values_list),
        "min": round(min(values_list), 3),
        "avg": round(statistics.mean(values_list), 3),
        "p50": _percentile(values_list, 0.50),
        "p90": _percentile(values_list, 0.90),
        "p95": _percentile(values_list, 0.95),
        "max": round(max(values_list), 3),
    }


def summarize_samples(
    samples: Sequence[Mapping[str, Any]],
    elapsed_s: float,
    configured_connections: int,
    peak_in_flight: int | None = None,
    average_in_flight: float | None = None,
) -> dict[str, Any]:
    """Aggregate samples into report-ready QPS/throughput/latency metrics."""

    elapsed = max(float(elapsed_s), 0.000001)
    application_error_counts = [
        int(sample.get("application_error_events", 0) or 0)
        or _count_error_events(sample.get("event_type_counts") or {})
        for sample in samples
    ]
    successes = [
        sample
        for sample, application_errors in zip(samples, application_error_counts)
        if bool(sample.get("success")) and application_errors == 0
    ]
    transport_successes = [
        sample
        for sample in samples
        if bool(sample.get("transport_success", sample.get("success")))
    ]
    errors = len(samples) - len(successes)
    event_count = sum(int(sample.get("events", 0) or 0) for sample in samples)
    event_type_counts: Counter[str] = Counter()
    for sample in samples:
        event_type_counts.update(sample.get("event_type_counts") or {})
    application_error_requests = sum(
        1
        for sample, count in zip(samples, application_error_counts)
        if count or sample.get("application_error")
    )
    stream_seconds = sum(float(sample.get("stream_ms", 0) or 0) for sample in samples) / 1000
    protocols = {
        str(sample.get("protocol"))
        for sample in samples
        if sample.get("protocol") not in (None, "")
    }
    protocol = next(iter(protocols)) if len(protocols) == 1 else ("mixed" if protocols else None)
    ttfb_values = [
        float(sample["first_byte_ms"])
        for sample in successes
        if sample.get("first_byte_ms") is not None
    ]
    ttfe_values = (
        [
            float(sample["first_event_ms"])
            for sample in successes
            if sample.get("first_event_ms") is not None
        ]
        if protocol == "sse"
        else []
    )
    latency_values = [
        float(sample["total_ms"])
        for sample in successes
        if sample.get("total_ms") is not None
    ]
    response_bytes = [float(sample.get("bytes_received", 0) or 0) for sample in successes]
    bytes_received = sum(int(sample.get("bytes_received", 0) or 0) for sample in samples)
    is_sse = protocol == "sse"
    event_rate = event_count / stream_seconds if is_sse and stream_seconds > 0 else None
    return {
        "requests": len(samples),
        "successes": len(successes),
        "errors": errors,
        "transport_successes": len(transport_successes),
        "transport_errors": len(samples) - len(transport_successes),
        "application_error_requests": application_error_requests,
        "success_rate": round(len(successes) / len(samples), 4) if samples else 0.0,
        "events": event_count,
        "event_type_counts": dict(event_type_counts),
        "qps": round(len(successes) / elapsed, 3),
        "event_throughput_events_per_s": round(event_count / elapsed, 3) if is_sse else None,
        "stream_event_rate_events_per_s": round(event_rate, 3) if event_rate is not None else None,
        "bytes_received": bytes_received,
        "byte_throughput_bytes_per_s": round(bytes_received / elapsed, 3),
        "response_bytes": _distribution(response_bytes),
        "protocol": protocol,
        "configured_connections": configured_connections,
        "connections": configured_connections,
        "peak_in_flight": peak_in_flight if peak_in_flight is not None else configured_connections,
        "average_in_flight": round(average_in_flight, 3) if average_in_flight is not None else None,
        "connection_mode": "fresh-per-request",
        "ttfb_ms": _distribution(ttfb_values),
        "ttfe_ms": _distribution(ttfe_values),
        "latency_ms": _distribution(latency_values),
    }


def _run_window(
    target: TargetConfig,
    scenario: ScenarioConfig,
    connections: int,
    duration_s: float,
    timeout_s: float,
    environ: Mapping[str, str],
    max_requests: int | None,
    worker_record: bool,
) -> tuple[float, list[dict[str, Any]], int, float]:
    started = time.perf_counter()
    deadline = started + duration_s
    tracker = _InFlight()
    samples: list[dict[str, Any]] = []
    samples_lock = Lock()
    request_lock = Lock()
    request_count = 0

    def claim_request() -> int | None:
        nonlocal request_count
        with request_lock:
            if max_requests is not None and request_count >= max_requests:
                return None
            request_index = request_count
            request_count += 1
            return request_index

    def worker(worker_id: int) -> None:
        while time.perf_counter() < deadline:
            request_index = claim_request()
            if request_index is None:
                return
            tracker.enter()
            try:
                sample = measure_request(
                    target,
                    scenario,
                    environ,
                    timeout_s,
                    worker_id=worker_id,
                    request_index=request_index,
                )
            finally:
                tracker.leave()
            if worker_record:
                with samples_lock:
                    samples.append(sample)

    with ThreadPoolExecutor(max_workers=connections) as executor:
        futures = [executor.submit(worker, worker_id) for worker_id in range(connections)]
        for future in futures:
            future.result()
    elapsed = max(time.perf_counter() - started, 0.000001)
    return elapsed, samples, tracker.peak, tracker.average(elapsed)


def _unsupported_result(
    target: TargetConfig,
    scenario: ScenarioConfig,
    connections: int,
    reason: str,
) -> dict[str, Any]:
    return {
        "target": target.name,
        "scenario": scenario.name,
        "status": "unsupported",
        "reason": reason,
        "configured_connections": connections,
        "connections": connections,
        "requests": 0,
        "successes": 0,
        "errors": 0,
        "events": 0,
        "protocol": scenario.protocol,
        "qps": None,
        "event_throughput_events_per_s": None,
        "stream_event_rate_events_per_s": None,
        "peak_in_flight": 0,
        "average_in_flight": 0.0,
        "connection_mode": "fresh-per-request",
        "ttfb_ms": _distribution([]),
        "ttfe_ms": _distribution([]),
        "latency_ms": _distribution([]),
    }


def run_benchmark(
    targets: Mapping[str, TargetConfig],
    scenarios: Sequence[str] = ("events", "empty_workflow"),
    connection_levels: Sequence[int] = (1, 4, 8),
    duration_s: float = 10.0,
    warmup_s: float = 2.0,
    timeout_s: float = 60.0,
    max_requests: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run every requested target/scenario/connection combination."""

    if duration_s <= 0:
        raise BenchmarkConfigError("duration_s must be greater than zero")
    if warmup_s < 0:
        raise BenchmarkConfigError("warmup_s cannot be negative")
    if timeout_s <= 0:
        raise BenchmarkConfigError("timeout_s must be greater than zero")
    if max_requests is not None and max_requests <= 0:
        raise BenchmarkConfigError("max_requests must be greater than zero")
    levels = tuple(int(level) for level in connection_levels)
    if not levels or any(level <= 0 for level in levels):
        raise BenchmarkConfigError("connection levels must contain positive integers")
    requested_scenarios = tuple(scenarios)
    if not requested_scenarios or any(name not in SCENARIO_NAMES for name in requested_scenarios):
        raise BenchmarkConfigError(
            f"scenarios must be chosen from {sorted(SCENARIO_NAMES)}"
        )
    env = dict(os.environ if environ is None else environ)
    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    results: list[dict[str, Any]] = []
    all_samples: list[dict[str, Any]] = []

    for target in targets.values():
        missing = target.missing_requirements(env)
        for scenario_name in requested_scenarios:
            scenario = target.scenario(scenario_name)
            for connections in levels:
                if not scenario.supported:
                    results.append(
                        _unsupported_result(
                            target,
                            scenario,
                            connections,
                            scenario.note or "scenario is not configured",
                        )
                    )
                    continue
                if missing:
                    result = _unsupported_result(
                        target,
                        scenario,
                        connections,
                        f"missing configuration: {', '.join(missing)}",
                    )
                    result["status"] = "skipped"
                    results.append(result)
                    continue
                if warmup_s:
                    _run_window(
                        target,
                        scenario,
                        connections,
                        warmup_s,
                        timeout_s,
                        env,
                        max_requests=None,
                        worker_record=False,
                    )
                elapsed, samples, peak, average = _run_window(
                    target,
                    scenario,
                    connections,
                    duration_s,
                    timeout_s,
                    env,
                    max_requests=max_requests,
                    worker_record=True,
                )
                summary = summarize_samples(samples, elapsed, connections, peak, average)
                summary.update(
                    {
                        "target": target.name,
                        "scenario": scenario.name,
                        "status": (
                            "ok"
                            if summary["successes"] == summary["requests"]
                            else "partial"
                            if summary["successes"]
                            else "error"
                        ),
                        "elapsed_s": round(elapsed, 3),
                        "configured_duration_s": duration_s,
                        "warmup_s": warmup_s,
                        "timeout_s": timeout_s,
                        "scenario_note": scenario.note,
                    }
                )
                results.append(summary)
                for sample in samples:
                    sample.update(
                        {
                            "target": target.name,
                            "scenario": scenario.name,
                            "configured_connections": connections,
                        }
                    )
                all_samples.extend(samples)

    report = {
        "schema_version": BENCHMARK_VERSION,
        "generated_at": started_at,
        "parameters": {
            "scenarios": list(requested_scenarios),
            "connection_levels": list(levels),
            "duration_s": duration_s,
            "warmup_s": warmup_s,
            "timeout_s": timeout_s,
            "max_requests": max_requests,
        },
        "results": results,
        "targets": describe_targets(targets, env),
    }
    return report, all_samples


def _scenario_description(scenario: ScenarioConfig) -> dict[str, Any]:
    return {
        "path": scenario.path,
        "method": scenario.method,
        "protocol": scenario.protocol,
        "supported": scenario.supported,
        "note": scenario.note,
    }


def describe_targets(
    targets: Mapping[str, TargetConfig], environ: Mapping[str, str]
) -> dict[str, Any]:
    """Return a credential-safe resolved target manifest."""

    description: dict[str, Any] = {}
    for name, target in targets.items():
        _, credential_env = target.resolve_api_key(environ)
        description[name] = {
            "base_url": target.base_url,
            "api_key_env": target.api_key_env,
            "resolved_api_key_env": credential_env,
            "credential_present": credential_env is not None,
            "auth_header": target.auth_header,
            "auth_scheme": target.auth_scheme,
            "required_env": list(target.required_env),
            "metadata": dict(target.metadata),
            "scenarios": {
                "events": _scenario_description(target.event),
                "empty_workflow": _scenario_description(target.empty_workflow),
            },
        }
    return description


def parse_connection_levels(raw: str) -> tuple[int, ...]:
    try:
        levels = tuple(int(item.strip()) for item in raw.split(",") if item.strip())
    except ValueError as exc:
        raise BenchmarkConfigError("--connections must be a comma-separated integer list") from exc
    if not levels or any(level <= 0 for level in levels):
        raise BenchmarkConfigError("--connections must contain positive integers")
    return levels


def _format_value(value: Any) -> str:
    return "-" if value is None else str(value)


def write_markdown_report(report: Mapping[str, Any], path: str | Path) -> None:
    """Write a compact human-readable report alongside JSON artifacts."""

    lines = [
        "# RAG API Benchmark",
        "",
        "指标定义：`QPS = 应用成功请求数 / 测量窗口秒数`；`TTFB` 为首个响应字节；`TTFE` 仅用于首次完整 SSE 事件。",
        "`Events/s` 是产品原生 SSE 分帧率，只能作同一产品/协议内诊断。当前每次请求新建连接，因此延迟包含连接建立开销。",
        "",
        "| Platform | Scenario | Protocol | Connections cfg/peak/avg | Status | QPS | TTFB p50/p95 (ms) | TTFE p50/p95 (ms) | End-to-end p50/p95 (ms) | Events/s* | App errors | Success/Requests |",
        "| --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in report.get("results", []):
        ttfb = row.get("ttfb_ms", {}) or {}
        ttfe = row.get("ttfe_ms", {}) or {}
        latency = row.get("latency_ms", {}) or {}
        lines.append(
            "| {target} | {scenario} | {protocol} | {connections} | {status} | {qps} | {ttfb50}/{ttfb95} | {ttfe50}/{ttfe95} | {latency50}/{latency95} | {throughput} | {app_errors} | {success}/{requests} |".format(
                target=row.get("target", "-"),
                scenario=row.get("scenario", "-"),
                protocol=row.get("protocol", "-"),
                connections="{}/{}/{}".format(
                    row.get("connections", "-"),
                    row.get("peak_in_flight", "-"),
                    _format_value(row.get("average_in_flight")),
                ),
                status=row.get("status", "-"),
                qps=_format_value(row.get("qps")),
                ttfb50=_format_value(ttfb.get("p50")),
                ttfb95=_format_value(ttfb.get("p95")),
                ttfe50=_format_value(ttfe.get("p50")),
                ttfe95=_format_value(ttfe.get("p95")),
                latency50=_format_value(latency.get("p50")),
                latency95=_format_value(latency.get("p95")),
                throughput=_format_value(row.get("event_throughput_events_per_s")),
                app_errors=row.get("application_error_requests", 0),
                success=row.get("successes", 0),
                requests=row.get("requests", 0),
            )
        )
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


def add_api_benchmark_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "api-benchmark",
        help="benchmark MOI, Dify, FastGPT and MaxKB API event/empty-workflow throughput",
    )
    parser.add_argument("--config", type=Path, help="optional JSON target override config")
    parser.add_argument(
        "--platforms",
        default="all",
        help="comma-separated target names or all (moi,dify,fastgpt,maxkb)",
    )
    parser.add_argument(
        "--scenario",
        choices=("events", "empty_workflow", "both"),
        default="both",
    )
    parser.add_argument("--connections", default="1,4,8")
    parser.add_argument("--duration", type=float, default=10.0, help="measurement seconds")
    parser.add_argument("--warmup", type=float, default=2.0, help="warm-up seconds")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout")
    parser.add_argument("--max-requests", type=int, help="optional safety cap per level")
    parser.add_argument("--question", help="override RAG_BENCHMARK_QUESTION")
    parser.add_argument("--output", type=Path, default=Path("runs/api-benchmark"))
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve endpoints and credential names without sending requests",
    )
    return parser


def run_api_benchmark_command(args: argparse.Namespace) -> int:
    environ = dict(os.environ)
    _load_benchmark_dotenv(environ)
    if args.question:
        environ["RAG_BENCHMARK_QUESTION"] = args.question
    targets = load_benchmark_targets(args.config, environ)
    if args.platforms.strip().lower() == "all":
        selected_names = list(targets)
    else:
        selected_names = [item.strip() for item in args.platforms.split(",") if item.strip()]
        unknown = [name for name in selected_names if name not in targets]
        if unknown:
            raise BenchmarkConfigError(f"unknown benchmark target(s): {', '.join(unknown)}")
    selected_targets = {name: targets[name] for name in selected_names}
    scenario_names = (
        ("events", "empty_workflow") if args.scenario == "both" else (args.scenario,)
    )
    connections = parse_connection_levels(args.connections)

    if args.dry_run:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "parameters": {
                        "platforms": selected_names,
                        "scenarios": list(scenario_names),
                        "connection_levels": list(connections),
                    },
                    "targets": describe_targets(selected_targets, environ),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    report, samples = run_benchmark(
        selected_targets,
        scenarios=scenario_names,
        connection_levels=connections,
        duration_s=args.duration,
        warmup_s=args.warmup,
        timeout_s=args.timeout,
        max_requests=args.max_requests,
        environ=environ,
    )
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (output / "samples.jsonl").open("w", encoding="utf-8") as handle:
        for sample in samples:
            handle.write(json.dumps(sample, ensure_ascii=False, sort_keys=True) + "\n")
    (output / "resolved-targets.json").write_text(
        json.dumps(report["targets"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_markdown_report(report, output / "report.md")
    print(f"benchmark artifacts: {output.resolve()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Standalone entry point used by local-rag-platforms/api/api_load_benchmark.py."""

    parser = argparse.ArgumentParser(prog="api_load_benchmark.py")
    subparsers = parser.add_subparsers(dest="command", required=True)
    add_api_benchmark_parser(subparsers)
    args = parser.parse_args(argv)
    if args.command == "api-benchmark":
        return run_api_benchmark_command(args)
    raise BenchmarkConfigError(f"unknown command: {args.command}")


__all__ = [
    "BenchmarkConfigError",
    "ScenarioConfig",
    "StreamEvent",
    "TargetConfig",
    "add_api_benchmark_parser",
    "build_builtin_targets",
    "describe_targets",
    "iter_sse_events",
    "load_benchmark_targets",
    "main",
    "measure_request",
    "parse_connection_levels",
    "render_template",
    "run_api_benchmark_command",
    "run_benchmark",
    "summarize_samples",
    "write_markdown_report",
]
