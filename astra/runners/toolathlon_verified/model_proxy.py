from __future__ import annotations

import argparse
import hashlib
import http.client
import hmac
import json
import os
import socket
import ssl
import struct
import sys
import threading
import time
from collections.abc import Mapping
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .artifact_contract import missing_observation, reported_observation
from .contract import (
    ContractError,
    canonical_json_sha256,
    utc_now,
    write_json_atomic,
)


_CHAT_PATHS = frozenset(
    {
        "/chat/completions",
        "/v1/chat/completions",
        "/responses",
        "/v1/responses",
    }
)
_MODEL_PATHS = frozenset({"/models", "/v1/models"})
PROVIDER_KEY_ENV_BY_SYSTEM = {
    "astra": "TOOLATHLON_DEEPSEEK_ASTRA_API_KEY",
    "hermes": "TOOLATHLON_DEEPSEEK_HERMES_API_KEY",
}
_LEGACY_SHARED_KEY_ENV = "DEEPSEEK_API_KEY"
POST_TERMINAL_MODEL_DRAIN_SECONDS = 120.0
POST_TERMINAL_MODEL_QUIET_SECONDS = 1.0
_UPSTREAM_POOL_MAX_CONNECTIONS = 4
_DIAGNOSTIC_TEXT_MAX = 1000
_DEFAULTED_GENERATION_FIELDS = frozenset(
    {
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "max_tokens",
        "max_completion_tokens",
        "reasoning_effort",
        "thinking",
        "seed",
        "logprobs",
        "top_logprobs",
        "tool_choice",
        "parallel_tool_calls",
    }
)


def _diagnostic_text(value: str, secret: str) -> str:
    return value.replace(secret, "[REDACTED]")[:_DIAGNOSTIC_TEXT_MAX]


def _exception_diagnostics(
    exc: BaseException, *, source: str, secret: str
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "source": source,
        "type": type(exc).__name__,
        "message": _diagnostic_text(str(exc), secret),
    }
    for field in ("errno", "library", "reason"):
        value = getattr(exc, field, None)
        if isinstance(value, (int, str)):
            result[field] = value
    if isinstance(exc, http.client.IncompleteRead):
        result["partial_bytes"] = len(exc.partial)
        result["expected_bytes"] = exc.expected
    return result


def _provider_error_diagnostics(
    error: dict[str, Any] | None, *, secret: str
) -> dict[str, Any] | None:
    if not error:
        return None
    result: dict[str, Any] = {}
    for field in ("code", "type", "param", "message"):
        value = error.get(field)
        if isinstance(value, str):
            result[field] = _diagnostic_text(value, secret)
        elif isinstance(value, (int, float, bool)):
            result[field] = value
    return result or None


def _reported_token(usage: dict[str, Any] | None, *paths: tuple[str, ...]) -> dict[str, Any]:
    if isinstance(usage, dict):
        for path in paths:
            value: Any = usage
            for key in path:
                if not isinstance(value, dict) or key not in value:
                    value = None
                    break
                value = value[key]
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return reported_observation(value, "provider_response." + ".".join(path))
    return missing_observation("provider_response", "provider_not_reported")


def _token_usage_observations(
    usage: dict[str, Any] | None, *, missing_reason: str = "provider_not_reported"
) -> dict[str, dict[str, Any]]:
    if usage is None and missing_reason != "provider_not_reported":
        missing = missing_observation("provider_response", missing_reason)
        return {
            "input_tokens": dict(missing),
            "output_tokens": dict(missing),
            "cache_read_tokens": dict(missing),
            "cache_write_tokens": dict(missing),
            "total_tokens": dict(missing),
        }
    return {
        "input_tokens": _reported_token(usage, ("prompt_tokens",), ("input_tokens",)),
        "output_tokens": _reported_token(
            usage, ("completion_tokens",), ("output_tokens",)
        ),
        "cache_read_tokens": _reported_token(
            usage,
            ("cache_read_tokens",),
            ("prompt_cache_hit_tokens",),
            ("prompt_tokens_details", "cached_tokens"),
        ),
        "cache_write_tokens": _reported_token(
            usage, ("cache_write_tokens",), ("prompt_cache_write_tokens",)
        ),
        "total_tokens": _reported_token(usage, ("total_tokens",)),
    }


def _finish_reason_observation(finish_reasons: list[str]) -> dict[str, Any]:
    if finish_reasons:
        return reported_observation(finish_reasons, "provider_response.choices.finish_reason")
    return missing_observation("provider_response", "provider_not_reported")


def _retry_observation() -> dict[str, Any]:
    return missing_observation("product_event", "product_retry_relation_not_exposed")


def _request_tool_names(body: dict[str, Any]) -> list[str]:
    """Extract model-visible tool names without retaining request content."""

    tools = body.get("tools")
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if isinstance(name, str) and name:
            names.append(name)
    return sorted(set(names))


def provider_credential_fingerprint(api_key: str) -> str:
    """Return a comparable identifier without persisting the provider secret."""

    if not api_key:
        raise ContractError("provider API key is empty")
    return f"sha256:{hashlib.sha256(api_key.encode('utf-8')).hexdigest()}"


def provider_key_environment(system_id: str) -> str:
    try:
        return PROVIDER_KEY_ENV_BY_SYSTEM[system_id]
    except KeyError as exc:
        raise ContractError(f"unsupported model-proxy system: {system_id}") from exc


def provider_user_id(system_id: str, run_id: str) -> str:
    """Derive a non-private, run-isolated DeepSeek scheduling/cache identity."""

    provider_key_environment(system_id)
    if not run_id:
        raise ContractError("model proxy run ID is empty")
    run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]
    return f"toolathlon-{system_id}-{run_digest}"


def load_system_provider_credential(
    system_id: str, environment: Mapping[str, str] | None = None
) -> tuple[str, str]:
    source = os.environ if environment is None else environment
    variable = provider_key_environment(system_id)
    value = source.get(variable, "")
    if not value:
        raise ContractError(f"missing provider credential env {variable}")
    has_control_character = any(
        ord(character) < 33 or ord(character) == 127 for character in value
    )
    if len(value) < 16 or has_control_character:
        raise ContractError(f"provider credential env {variable} has an invalid value")
    return variable, value


def load_distinct_provider_credentials(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Load the frozen Astra/Hermes key pair and reject shared credentials."""

    source = os.environ if environment is None else environment
    if source.get(_LEGACY_SHARED_KEY_ENV):
        raise ContractError(
            "legacy shared DEEPSEEK_API_KEY is forbidden; use both frozen per-system variables"
        )
    credentials = {
        system_id: load_system_provider_credential(system_id, source)[1]
        for system_id in sorted(PROVIDER_KEY_ENV_BY_SYSTEM)
    }
    if hmac.compare_digest(credentials["astra"], credentials["hermes"]):
        raise ContractError("Astra and Hermes provider credentials must be different values")
    return credentials


def wait_for_model_requests_to_settle(
    snapshot: Callable[[], dict[str, Any]],
    *,
    timeout_seconds: float = POST_TERMINAL_MODEL_DRAIN_SECONDS,
    quiet_seconds: float = POST_TERMINAL_MODEL_QUIET_SECONDS,
    poll_seconds: float = 0.05,
    context: str = "Model Proxy",
) -> dict[str, Any]:
    """Wait for admitted model requests and adjacent background starts to close."""

    started = time.monotonic()
    deadline = started + timeout_seconds
    stable_since: float | None = None
    previous: tuple[int, int] | None = None
    while True:
        latest = snapshot()
        forwarded = latest.get("provider_requests_forwarded")
        completed = latest.get("provider_requests_completed")
        if (
            not isinstance(forwarded, int)
            or not isinstance(completed, int)
            or forwarded < 0
            or completed < 0
            or completed > forwarded
        ):
            raise ContractError(
                f"invalid Model Proxy request snapshot during {context} drain"
            )
        now = time.monotonic()
        current = (forwarded, completed)
        if current != previous:
            previous = current
            stable_since = now if completed == forwarded else None
        elif completed == forwarded:
            stable_since = stable_since or now
        else:
            stable_since = None
        if stable_since is not None and now - stable_since >= quiet_seconds:
            return {
                "settled": True,
                "timeout_seconds": timeout_seconds,
                "quiet_seconds": quiet_seconds,
                "wait_seconds": round(now - started, 6),
                "provider_requests_forwarded": forwarded,
                "provider_requests_completed": completed,
            }
        if now >= deadline:
            return {
                "settled": False,
                "timeout_seconds": timeout_seconds,
                "quiet_seconds": quiet_seconds,
                "wait_seconds": round(now - started, 6),
                "provider_requests_forwarded": forwarded,
                "provider_requests_completed": completed,
            }
        time.sleep(min(poll_seconds, max(0.0, deadline - now)))


@dataclass(frozen=True)
class ModelProxyConfig:
    upstream_base_url: str
    upstream_api_key: str = field(repr=False)
    effective_model: str
    temperature: float
    thinking: str
    reasoning_effort: str
    max_requests: int
    run_id: str
    system_id: str
    events_path: Path
    state_path: Path

    def __post_init__(self) -> None:
        provider_key_environment(self.system_id)
        parsed = urlparse(self.upstream_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ContractError("model proxy upstream must be an http(s) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ContractError("model proxy upstream URL contains forbidden components")
        if not self.upstream_api_key:
            raise ContractError("model proxy upstream API key is empty")
        if self.effective_model != "deepseek-v4-flash":
            raise ContractError("model proxy must freeze deepseek-v4-flash")
        if self.temperature != 0.0:
            raise ContractError("model proxy must freeze temperature=0")
        if self.thinking != "enabled":
            raise ContractError("model proxy must freeze thinking=enabled")
        if self.reasoning_effort != "max":
            raise ContractError("model proxy must freeze reasoning_effort=max")
        if self.max_requests != 100:
            raise ContractError("model proxy must freeze max_requests=100")

    @property
    def provider_credential_fingerprint(self) -> str:
        return provider_credential_fingerprint(self.upstream_api_key)

    @property
    def provider_user_id(self) -> str:
        return provider_user_id(self.system_id, self.run_id)


class RequestBudget:
    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self._lock = threading.Lock()
        self._forwarded = 0
        self._attempted = 0
        self._completed = 0
        self._failed = 0
        self._limit_rejections = 0
        self.exceeded = threading.Event()

    def reserve(self) -> tuple[bool, int, int]:
        with self._lock:
            self._attempted += 1
            attempt = self._attempted
            if self._forwarded >= self.maximum:
                self._limit_rejections += 1
                self.exceeded.set()
                return False, attempt, self._forwarded
            self._forwarded += 1
            return True, attempt, self._forwarded

    def finish(self, *, provider_request: int, success: bool) -> None:
        with self._lock:
            self._completed += 1
            if not success:
                self._failed += 1
            # The 100th admitted request is allowed to return in full. Once it
            # reaches a terminal response, the Agent slot must stop even if the
            # product would otherwise continue using tools without requesting
            # another model response.
            if provider_request >= self.maximum:
                self.exceeded.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "max_requests": self.maximum,
                "product_attempts": self._attempted,
                "provider_requests_forwarded": self._forwarded,
                "provider_requests_completed": self._completed,
                "provider_requests_failed": self._failed,
                "limit_rejections": self._limit_rejections,
                "limit_exceeded": self.exceeded.is_set(),
            }


class _AuditLog:
    def __init__(self, config: ModelProxyConfig, budget: RequestBudget) -> None:
        self.config = config
        self.budget = budget
        self._lock = threading.Lock()
        config.events_path.parent.mkdir(parents=True, exist_ok=True)
        if config.events_path.exists() and config.events_path.stat().st_size:
            raise ContractError("model usage event file must start empty")
        self.write_state()

    def append(self, event: str, **fields: Any) -> None:
        row = {
            "schema_version": "toolathlon.model-proxy.events.v1",
            "timestamp": utc_now(),
            "monotonic_ns": time.monotonic_ns(),
            "run_id": self.config.run_id,
            "system_id": self.config.system_id,
            "event": event,
            **fields,
        }
        with self._lock:
            with self.config.events_path.open("a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            self.write_state()

    def write_state(self) -> None:
        write_json_atomic(
            self.config.state_path,
            {
                "schema_version": 1,
                "run_id": self.config.run_id,
                "system_id": self.config.system_id,
                "updated_at": utc_now(),
                "provider_identity": {
                    "credential_environment": provider_key_environment(
                        self.config.system_id
                    ),
                    "credential_fingerprint": self.config.provider_credential_fingerprint,
                    "user_id": self.config.provider_user_id,
                },
                "budget": self.budget.snapshot(),
            },
            mode=0o600,
        )


class _UsageProbe:
    """Retain only a bounded response tail and extract provider usage."""

    def __init__(self, maximum: int = 2 * 1024 * 1024) -> None:
        self.maximum = maximum
        self.tail = bytearray()

    def feed(self, chunk: bytes) -> None:
        self.tail.extend(chunk)
        if len(self.tail) > self.maximum:
            del self.tail[: len(self.tail) - self.maximum]

    def metadata(self) -> dict[str, Any]:
        raw = bytes(self.tail)
        candidates: list[dict[str, Any]] = []
        try:
            value = json.loads(raw.decode("utf-8"))
            if isinstance(value, dict):
                candidates.append(value)
        except (UnicodeDecodeError, json.JSONDecodeError):
            for line in raw.decode("utf-8", errors="ignore").splitlines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    value = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    candidates.append(value)
        usage: dict[str, Any] | None = None
        provider_response_id: str | None = None
        provider_error: dict[str, Any] | None = None
        finish_reasons: list[str] = []
        for value in candidates:
            response_id = value.get("id")
            if isinstance(response_id, str) and response_id:
                provider_response_id = response_id
            error = value.get("error")
            if isinstance(error, dict):
                provider_error = error
            choices = value.get("choices")
            if isinstance(choices, list):
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    reason = choice.get("finish_reason")
                    if isinstance(reason, str) and reason not in finish_reasons:
                        finish_reasons.append(reason)
        for value in reversed(candidates):
            usage = value.get("usage")
            if isinstance(usage, dict):
                break
            usage = None
        return {
            "usage": usage,
            "provider_response_id": provider_response_id,
            "provider_error": provider_error,
            "finish_reasons": finish_reasons,
        }


class _UpstreamConnectionPool:
    def __init__(
        self,
        factory: Callable[[], http.client.HTTPConnection],
        *,
        maximum: int,
    ) -> None:
        self._factory = factory
        self._maximum = maximum
        self._condition = threading.Condition()
        self._idle: list[http.client.HTTPConnection] = []
        self._created = 0
        self._closed = False

    def acquire(self) -> tuple[http.client.HTTPConnection, bool]:
        with self._condition:
            while True:
                if self._closed:
                    raise RuntimeError("upstream connection pool is closed")
                if self._idle:
                    return self._idle.pop(), True
                if self._created < self._maximum:
                    self._created += 1
                    break
                self._condition.wait()
        try:
            return self._factory(), False
        except BaseException:
            with self._condition:
                self._created -= 1
                self._condition.notify()
            raise

    def release(
        self, connection: http.client.HTTPConnection, *, reusable: bool
    ) -> None:
        close = not reusable
        with self._condition:
            if reusable and not self._closed:
                self._idle.append(connection)
            else:
                self._created -= 1
                close = True
            self._condition.notify()
        if close:
            connection.close()

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            idle = self._idle
            self._idle = []
            self._created -= len(idle)
            self._condition.notify_all()
        for connection in idle:
            connection.close()


class _ModelProxyHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
            return
        super().handle_error(request, client_address)


class ModelProxyServer:
    def __init__(self, config: ModelProxyConfig, *, host: str = "127.0.0.1", port: int = 0):
        self.config = config
        self.budget = RequestBudget(config.max_requests)
        self.audit = _AuditLog(config, self.budget)
        parsed = urlparse(config.upstream_base_url)
        if parsed.scheme == "https":
            context = ssl.create_default_context()

            def connection_factory() -> http.client.HTTPConnection:
                return http.client.HTTPSConnection(
                    parsed.hostname, parsed.port, context=context
                )

        else:

            def connection_factory() -> http.client.HTTPConnection:
                return http.client.HTTPConnection(parsed.hostname, parsed.port)

        self.upstream_pool = _UpstreamConnectionPool(
            connection_factory, maximum=_UPSTREAM_POOL_MAX_CONNECTIONS
        )
        handler = self._handler_type()
        self.server = _ModelProxyHTTPServer((host, port), handler)
        self.server.daemon_threads = True
        self.thread: threading.Thread | None = None

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        config = self.config
        budget = self.budget
        audit = self.audit
        upstream_pool = self.upstream_pool

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "ToolathlonModelProxy/1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _abort_downstream(self) -> None:
                self.close_connection = True
                try:
                    self.connection.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_LINGER,
                        struct.pack("ii", 1, 0),
                    )
                except OSError:
                    pass
                self.connection.close()

            def _json(self, status: int, value: Any) -> None:
                payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path in {"/health", "/ready"}:
                    self._json(
                        200,
                        {
                            "status": "ok",
                            "model": config.effective_model,
                            "budget": budget.snapshot(),
                        },
                    )
                    return
                if path in _MODEL_PATHS:
                    self._json(
                        200,
                        {
                            "object": "list",
                            "data": [
                                {
                                    "id": config.effective_model,
                                    "object": "model",
                                    "owned_by": "deepseek",
                                }
                            ],
                        },
                    )
                    return
                self._json(404, {"error": {"code": "proxy_path_not_allowed"}})

            def do_POST(self) -> None:  # noqa: N802
                self._response_started = False
                path = self.path.split("?", 1)[0]
                if path not in _CHAT_PATHS:
                    self._json(404, {"error": {"code": "proxy_path_not_allowed"}})
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._json(400, {"error": {"code": "invalid_content_length"}})
                    return
                if length <= 0 or length > 64 * 1024 * 1024:
                    self._json(413, {"error": {"code": "invalid_request_size"}})
                    return
                raw = self.rfile.read(length)
                try:
                    body = json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._json(400, {"error": {"code": "invalid_json"}})
                    return
                if not isinstance(body, dict):
                    self._json(400, {"error": {"code": "request_must_be_object"}})
                    return

                admitted, product_attempt, provider_request = budget.reserve()
                requested_model = body.get("model")
                if not admitted:
                    rejected_request_id = f"{config.run_id}:model-attempt:{product_attempt}"
                    audit.append(
                        "model_request.rejected_limit",
                        model_request_id=rejected_request_id,
                        product_attempt=product_attempt,
                        provider_requests_forwarded=provider_request,
                        requested_model=requested_model,
                        effective_model=config.effective_model,
                        retry_of=_retry_observation(),
                        finish_reason=missing_observation(
                            "provider_response", "request_not_forwarded"
                        ),
                        token_usage=_token_usage_observations(
                            None, missing_reason="request_not_forwarded"
                        ),
                        raw_provider_usage=missing_observation(
                            "provider_response", "request_not_forwarded"
                        ),
                    )
                    self._json(
                        429,
                        {
                            "error": {
                                "type": "benchmark_budget_exceeded",
                                "code": "max_model_requests",
                                "message": "frozen product model request budget exhausted",
                            }
                        },
                    )
                    return

                normalized, removed_generation, removed_identity = _normalize_request(
                    body, config
                )
                request_tool_names = _request_tool_names(normalized)
                request_hash = canonical_json_sha256(normalized)
                model_request_id = f"{config.run_id}:model:{provider_request}"
                audit.append(
                    "model_request.started",
                    model_request_id=model_request_id,
                    product_attempt=product_attempt,
                    provider_request=provider_request,
                    requested_model=requested_model,
                    effective_model=config.effective_model,
                    temperature_sent=config.temperature,
                    temperature_effective=False,
                    thinking=config.thinking,
                    thinking_wire_behavior="sent",
                    reasoning_effort=config.reasoning_effort,
                    reasoning_effort_wire_behavior="sent",
                    generation_parameter_source="benchmark_override",
                    provider_user_id=config.provider_user_id,
                    removed_generation_parameters=removed_generation,
                    removed_identity_parameters=removed_identity,
                    request_sha256=request_hash,
                    request_tool_count=len(request_tool_names),
                    request_tool_names=request_tool_names,
                    request_tool_names_sha256=canonical_json_sha256(
                        request_tool_names
                    ),
                    retry_of=_retry_observation(),
                    retry_relation_reliability="not_exposed_by_product",
                    stream=bool(normalized.get("stream")),
                )
                started = time.monotonic()
                status = 502
                success = False
                usage: dict[str, Any] | None = None
                provider_response_id: str | None = None
                provider_header_request_id: str | None = None
                finish_reasons: list[str] = []
                error_type: str | None = None
                transport_diagnostics: dict[str, Any] = {
                    "phase": "not_started",
                    "connection_reused": None,
                    "upstream_http_status": None,
                    "upstream_content_type": None,
                    "upstream_content_length": None,
                    "upstream_chunked": None,
                    "upstream_will_close": None,
                    "upstream_body_bytes_read": 0,
                    "provider_request_id": None,
                    "provider_response_id": None,
                    "provider_error": None,
                    "exception": None,
                }
                try:
                    status, response_metadata = self._forward(
                        path, normalized, transport_diagnostics
                    )
                    usage = response_metadata["usage"]
                    provider_response_id = response_metadata["provider_response_id"]
                    provider_header_request_id = response_metadata[
                        "provider_header_request_id"
                    ]
                    finish_reasons = response_metadata["finish_reasons"]
                    success = 200 <= status < 300
                except (BrokenPipeError, ConnectionResetError) as exc:
                    error_type = "downstream_disconnected"
                    source = (
                        "downstream"
                        if str(transport_diagnostics["phase"]).startswith(
                            "write_downstream"
                        )
                        else "upstream"
                    )
                    transport_diagnostics["exception"] = _exception_diagnostics(
                        exc, source=source, secret=config.upstream_api_key
                    )
                    self.close_connection = True
                except BaseException as exc:
                    error_type = type(exc).__name__
                    source = (
                        "downstream"
                        if str(transport_diagnostics["phase"]).startswith(
                            "write_downstream"
                        )
                        else "upstream"
                    )
                    transport_diagnostics["exception"] = _exception_diagnostics(
                        exc, source=source, secret=config.upstream_api_key
                    )
                    if self._response_started:
                        self._abort_downstream()
                    elif not self.wfile.closed:
                        try:
                            self._json(
                                502,
                                {"error": {"code": "provider_transport_error"}},
                            )
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                finally:
                    provider_response_id = (
                        provider_response_id
                        or transport_diagnostics["provider_response_id"]
                    )
                    provider_header_request_id = (
                        provider_header_request_id
                        or transport_diagnostics["provider_request_id"]
                    )
                    budget.finish(
                        provider_request=provider_request,
                        success=success,
                    )
                    audit.append(
                        "model_request.completed",
                        model_request_id=model_request_id,
                        product_attempt=product_attempt,
                        provider_request=provider_request,
                        http_status=status,
                        success=success,
                        duration_seconds=round(time.monotonic() - started, 6),
                        provider_response_id=(
                            reported_observation(
                                provider_response_id, "provider_response.id"
                            )
                            if provider_response_id
                            else missing_observation(
                                "provider_response", "provider_not_reported"
                            )
                        ),
                        provider_header_request_id=(
                            reported_observation(
                                provider_header_request_id,
                                "provider_response.headers.request_id",
                            )
                            if provider_header_request_id
                            else missing_observation(
                                "provider_response", "provider_not_reported"
                            )
                        ),
                        finish_reasons=finish_reasons,
                        finish_reason=_finish_reason_observation(finish_reasons),
                        usage=(
                            reported_observation(usage, "provider_response.usage")
                            if usage is not None
                            else missing_observation(
                                "provider_response", "provider_not_reported"
                            )
                        ),
                        raw_provider_usage=(
                            reported_observation(usage, "provider_response.usage")
                            if usage is not None
                            else missing_observation(
                                "provider_response", "provider_not_reported"
                            )
                        ),
                        usage_reliability="reported" if usage is not None else "missing",
                        token_usage=_token_usage_observations(usage),
                        retry_of=_retry_observation(),
                        transport_diagnostics=transport_diagnostics,
                        error_type=(
                            reported_observation(error_type, "model_proxy")
                            if error_type
                            else missing_observation("model_proxy", "no_transport_error")
                        ),
                    )

            def _forward(
                self,
                request_path: str,
                body: dict[str, Any],
                diagnostics: dict[str, Any],
            ) -> tuple[int, dict[str, Any]]:
                parsed = urlparse(config.upstream_base_url)
                prefix = parsed.path.rstrip("/")
                normalized_path = request_path
                if normalized_path.startswith("/v1/"):
                    normalized_path = normalized_path[3:]
                target = f"{prefix}{normalized_path}" or "/"
                diagnostics["phase"] = "acquire_upstream_connection"
                connection, connection_reused = upstream_pool.acquire()
                diagnostics["connection_reused"] = connection_reused
                reusable = False
                probe = _UsageProbe()
                payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
                try:
                    diagnostics["phase"] = "send_upstream_request"
                    connection.request(
                        "POST",
                        target,
                        body=payload,
                        headers={
                            "Authorization": f"Bearer {config.upstream_api_key}",
                            "Content-Type": "application/json",
                            "Accept": "text/event-stream, application/json",
                            "User-Agent": "toolathlon-astra-hermes-evaluation/1",
                        },
                    )
                    diagnostics["phase"] = "read_upstream_headers"
                    upstream = connection.getresponse()
                    status = upstream.status
                    content_type = upstream.getheader("Content-Type", "application/json")
                    content_length = upstream.getheader("Content-Length")
                    provider_header_request_id = upstream.getheader(
                        "x-request-id"
                    ) or upstream.getheader("x-ds-request-id")
                    diagnostics["upstream_http_status"] = status
                    diagnostics["upstream_content_type"] = content_type
                    diagnostics["upstream_content_length"] = (
                        int(content_length)
                        if content_length and content_length.isdecimal()
                        else None
                    )
                    diagnostics["upstream_chunked"] = upstream.chunked
                    diagnostics["upstream_will_close"] = upstream.will_close
                    diagnostics["provider_request_id"] = provider_header_request_id
                    chunked = content_length is None
                    diagnostics["phase"] = "write_downstream_headers"
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Cache-Control", "no-store")
                    if chunked:
                        self.send_header("Transfer-Encoding", "chunked")
                    else:
                        self.send_header("Content-Length", content_length)
                    # Declaring Connection: close makes a truncated SSE body look
                    # like a clean EOF to Node/Undici, hiding the transport error.
                    self.end_headers()
                    self._response_started = True
                    diagnostics["phase"] = "read_upstream_body"
                    while True:
                        chunk = upstream.read1(64 * 1024)
                        if not chunk:
                            break
                        diagnostics["upstream_body_bytes_read"] += len(chunk)
                        probe.feed(chunk)
                        diagnostics["phase"] = "write_downstream_body"
                        if chunked:
                            self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                            self.wfile.write(chunk)
                            self.wfile.write(b"\r\n")
                        else:
                            self.wfile.write(chunk)
                        self.wfile.flush()
                        diagnostics["phase"] = "read_upstream_body"
                    upstream.close()
                    if chunked:
                        diagnostics["phase"] = "write_downstream_terminator"
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    reusable = not upstream.will_close and connection.sock is not None
                    metadata = probe.metadata()
                    metadata["provider_header_request_id"] = provider_header_request_id
                    diagnostics["phase"] = "complete"
                    return status, metadata
                finally:
                    metadata = probe.metadata()
                    diagnostics["provider_response_id"] = metadata[
                        "provider_response_id"
                    ]
                    diagnostics["provider_error"] = _provider_error_diagnostics(
                        metadata["provider_error"], secret=config.upstream_api_key
                    )
                    upstream_pool.release(connection, reusable=reusable)

        return Handler

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def start(self) -> None:
        if self.thread is not None:
            raise ContractError("model proxy already started")
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="toolathlon-model-proxy",
            daemon=True,
        )
        self.thread.start()
        self.audit.append(
            "proxy.ready",
            listen_url=self.url,
            effective_model=self.config.effective_model,
            upstream_origin=urlparse(self.config.upstream_base_url).netloc,
            provider_credential_environment=provider_key_environment(
                self.config.system_id
            ),
            provider_credential_fingerprint=self.config.provider_credential_fingerprint,
            provider_user_id=self.config.provider_user_id,
        )

    def close(self) -> None:
        if self.thread is None:
            return
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.thread = None
        self.upstream_pool.close()
        self.audit.append("proxy.stopped")

    def __enter__(self) -> "ModelProxyServer":
        self.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


def _normalize_request(
    body: dict[str, Any], config: ModelProxyConfig
) -> tuple[dict[str, Any], list[str], list[str]]:
    normalized = dict(body)
    removed_generation: list[str] = []
    removed_identity: list[str] = []
    for key in sorted(_DEFAULTED_GENERATION_FIELDS):
        if key in normalized:
            normalized.pop(key, None)
            removed_generation.append(key)
    if "user_id" in normalized:
        normalized.pop("user_id", None)
        removed_identity.append("user_id")
    extra_body = normalized.get("extra_body")
    if isinstance(extra_body, dict):
        rewritten_extra = dict(extra_body)
        for key in sorted(_DEFAULTED_GENERATION_FIELDS):
            if key in rewritten_extra:
                rewritten_extra.pop(key, None)
                removed_generation.append(f"extra_body.{key}")
        if "user_id" in rewritten_extra:
            rewritten_extra.pop("user_id", None)
            removed_identity.append("extra_body.user_id")
        if rewritten_extra:
            normalized["extra_body"] = rewritten_extra
        else:
            normalized.pop("extra_body", None)
    normalized["model"] = config.effective_model
    normalized["temperature"] = config.temperature
    normalized["user_id"] = config.provider_user_id
    normalized["thinking"] = {"type": config.thinking}
    normalized["reasoning_effort"] = config.reasoning_effort
    return normalized, removed_generation, removed_identity


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the frozen DeepSeek request proxy")
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream-base-url", default="https://api.deepseek.com")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--system-id", choices=("astra", "hermes"), required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    _api_key_env, api_key = load_system_provider_credential(args.system_id)
    config = ModelProxyConfig(
        upstream_base_url=args.upstream_base_url,
        upstream_api_key=api_key,
        effective_model="deepseek-v4-flash",
        temperature=0.0,
        thinking="enabled",
        reasoning_effort="max",
        max_requests=100,
        run_id=args.run_id,
        system_id=args.system_id,
        events_path=args.events,
        state_path=args.state,
    )
    server = ModelProxyServer(config, host=args.listen_host, port=args.listen_port)
    server.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 130
    finally:
        server.close()


if __name__ == "__main__":
    raise SystemExit(main())
