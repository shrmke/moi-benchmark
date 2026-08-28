#!/usr/bin/env python3
"""Read-only verify and explicit registration for MaxKB's split text/embedding models.

The helper is deliberately separate from the historical Qianfan helper.  A
MaxKB model is selected only when provider, model type, and model name all
match exactly; an old Qianfan/deepseek record is never a fallback.  `register`
is a dry run by default and only sends POST requests with `--execute`.

MaxKB 2.10.4 cannot persist nested ``thinking`` parameters in its native
OpenAI model form.  This helper therefore reports external generation as the
safe runner mode for the pinned release and exposes a separate external chat
payload with thinking disabled.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


PLATFORM_ROOT = Path(__file__).resolve().parents[1]
ROOT = PLATFORM_ROOT.parent
if str(PLATFORM_ROOT) not in sys.path:
    sys.path.insert(0, str(PLATFORM_ROOT))

DEFAULT_MAXKB_BASE_URL = "http://127.0.0.1:8090"
DEFAULT_MAAS_BASE_URL = "https://api.modelarts-maas.com/v1"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_MAAS_LLM_MODEL = "deepseek-v4-flash"
DEFAULT_MAAS_EMBEDDING_MODEL = "bge-m3"
DEFAULT_MAAS_EMBEDDING_DIMENSION = 1024
MAAS_HOST = "api.modelarts-maas.com"
DEEPSEEK_HOST = "api.deepseek.com"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY_NEW"
MAXKB_PINNED_VERSION = "2.10.4"
MAXKB_OPENAI_PROVIDER = "model_openai_provider"
MAXKB_LLM_TYPE = "LLM"
MAXKB_EMBEDDING_TYPE = "EMBEDDING"


class MaxKBMaaSError(RuntimeError):
    """A safe, user-facing configuration or provider contract error."""


def _normalise_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _validate_maas_base_url(value: str) -> str:
    """Accept only the frozen Huawei MaaS HTTPS endpoint."""

    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise MaxKBMaaSError("MAAS_ONLY_BASE_URL_REQUIRED") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != MAAS_HOST
        or port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise MaxKBMaaSError("MAAS_ONLY_BASE_URL_REQUIRED")
    return raw.rstrip("/")


def _validate_deepseek_base_url(value: str) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise MaxKBMaaSError("DEEPSEEK_OFFICIAL_BASE_URL_REQUIRED") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != DEEPSEEK_HOST
        or port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise MaxKBMaaSError("DEEPSEEK_OFFICIAL_BASE_URL_REQUIRED")
    return raw.rstrip("/")


def _required(value: str | None, name: str) -> str:
    value = str(value or "").strip()
    if not value or value.startswith("<"):
        raise MaxKBMaaSError(f"{name}_MISSING")
    return value


def _safe_model(record: Mapping[str, Any]) -> dict[str, Any]:
    """Return model metadata only; credential fields are intentionally excluded."""

    fields = (
        "id",
        "name",
        "model_name",
        "model_type",
        "provider",
        "status",
        "meta",
        "model_params_form",
        "workspace_id",
    )
    return {field: record.get(field) for field in fields if field in record}


def _redact(value: Any) -> Any:
    if isinstance(value, Mapping):
        secret_names = {"api_key", "apikey", "authorization", "token", "password", "secret"}
        return {
            str(key): "<redacted>" if str(key).casefold() in secret_names else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _list_records(payload: Any) -> list[dict[str, Any]]:
    data: Any = payload
    if isinstance(payload, Mapping):
        data = payload.get("data", payload)
    if isinstance(data, Mapping):
        data = data.get("list", data.get("results", data.get("items", [])))
    if not isinstance(data, list):
        raise MaxKBMaaSError("MAXKB_MODEL_LIST_INVALID")
    return [dict(item) for item in data if isinstance(item, Mapping)]


def _model_id(record: Mapping[str, Any]) -> str:
    return str(record.get("id") or record.get("model_id") or "").strip()


def _model_params(value: Any) -> list[dict[str, Any]]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MaxKBMaaSError("MAXKB_MODEL_PARAMS_INVALID") from exc
    if not isinstance(value, list):
        raise MaxKBMaaSError("MAXKB_MODEL_PARAMS_INVALID")
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _field_values(record: Mapping[str, Any], field: str) -> list[Any]:
    return [
        item.get("value")
        for item in _model_params(record.get("model_params_form"))
        if str(item.get("field") or "").strip().casefold() == field.casefold()
    ]


def validate_model_record(
    record: Mapping[str, Any],
    *,
    provider: str,
    model_type: str,
    model_name: str,
) -> dict[str, Any]:
    """Validate a selected record without ever accepting a near match."""

    record_id = _model_id(record)
    if not record_id:
        raise MaxKBMaaSError("MAXKB_MODEL_ID_MISSING")
    actual_provider = str(record.get("provider") or "").strip()
    actual_type = str(record.get("model_type") or "").strip().upper()
    actual_name = str(record.get("model_name") or "").strip()
    if actual_provider != provider:
        raise MaxKBMaaSError("MAXKB_PROVIDER_MISMATCH")
    if actual_type != model_type.upper():
        raise MaxKBMaaSError("MAXKB_MODEL_TYPE_MISMATCH")
    if actual_name.casefold() != model_name.casefold():
        raise MaxKBMaaSError("MAXKB_MODEL_NAME_MISMATCH")
    status = str(record.get("status") or "").strip().upper()
    if status and status not in {"SUCCESS", "READY", "AVAILABLE"}:
        raise MaxKBMaaSError(f"MAXKB_MODEL_STATUS:{status}")

    if actual_type == MAXKB_EMBEDDING_TYPE:
        dimensions = _field_values(record, "dimensions")
        for value in dimensions:
            try:
                if int(value) != DEFAULT_MAAS_EMBEDDING_DIMENSION:
                    raise MaxKBMaaSError("MAXKB_MAAS_EMBEDDING_DIMENSION_MISMATCH")
            except (TypeError, ValueError) as exc:
                raise MaxKBMaaSError("MAXKB_MAAS_EMBEDDING_DIMENSION_INVALID") from exc
    if actual_type == MAXKB_LLM_TYPE:
        for value in _field_values(record, "thinking"):
            if value not in ({"type": "disabled"}, '{"type":"disabled"}', '{"type": "disabled"}'):
                raise MaxKBMaaSError("MAXKB_MAAS_THINKING_PARAMETER_MISMATCH")
    return _safe_model(record)


def select_model_record(
    records: Iterable[Mapping[str, Any]],
    *,
    provider: str,
    model_type: str,
    model_name: str,
    model_id: str | None = None,
) -> dict[str, Any]:
    """Select by exact provider + type + name, requiring an ID for duplicates."""

    candidates = [
        dict(record)
        for record in records
        if isinstance(record, Mapping)
        and str(record.get("provider") or "").strip() == provider
        and str(record.get("model_type") or "").strip().upper() == model_type.upper()
        and str(record.get("model_name") or "").strip().casefold() == model_name.casefold()
    ]
    if model_id:
        selected = next((item for item in candidates if _model_id(item) == model_id), None)
        if selected is None:
            raise MaxKBMaaSError(f"MAXKB_MODEL_ID_NOT_FOUND:{model_id}")
        return selected
    if not candidates:
        raise MaxKBMaaSError(f"MAXKB_MODEL_NOT_REGISTERED:{provider}:{model_type}:{model_name}")
    if len(candidates) > 1:
        raise MaxKBMaaSError("MAXKB_MODEL_AMBIGUOUS_SET_MODEL_ID")
    return candidates[0]


def build_registration_payloads(
    *,
    api_key: str,
    base_url: str,
    supports_nested_thinking: bool = False,
    llm_api_key: str | None = None,
    llm_base_url: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Build DeepSeek text + Huawei MaaS bge-m3 records."""

    embedding_base_url = _validate_maas_base_url(base_url)
    generation_base_url = _validate_deepseek_base_url(llm_base_url or DEFAULT_DEEPSEEK_BASE_URL)
    llm_params = (
        [{"field": "thinking", "value": {"type": "disabled"}}]
        if supports_nested_thinking
        else []
    )
    generation_credential = {
        "api_base": _normalise_url(generation_base_url),
        "api_key": llm_api_key or f"<{DEEPSEEK_API_KEY_ENV}>",
    }
    embedding_credential = {"api_base": _normalise_url(embedding_base_url), "api_key": api_key}
    return {
        "llm": {
            "name": "DeepSeek Official deepseek-v4-flash",
            "model_type": MAXKB_LLM_TYPE,
            "model_name": DEFAULT_MAAS_LLM_MODEL,
            "model_params_form": llm_params,
            "credential": generation_credential,
            "provider": MAXKB_OPENAI_PROVIDER,
        },
        "embedding": {
            "name": "Huawei MaaS bge-m3",
            "model_type": MAXKB_EMBEDDING_TYPE,
            "model_name": DEFAULT_MAAS_EMBEDDING_MODEL,
            "model_params_form": [{"field": "dimensions", "value": str(DEFAULT_MAAS_EMBEDDING_DIMENSION)}],
            "credential": embedding_credential,
            "provider": MAXKB_OPENAI_PROVIDER,
        },
    }


class JsonHttpClient:
    """Small dependency-free JSON client with credential-safe failures."""

    def __init__(self, base_url: str, *, api_key: str | None = None, timeout: float = 60.0):
        self.base_url = _normalise_url(base_url)
        self.api_key = api_key
        self.timeout = timeout

    def request(self, method: str, path: str, *, body: Any = None) -> Any:
        headers = {"Accept": "application/json"}
        data: bytes | None = None
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/{path.lstrip('/')}",
            data=data,
            headers=headers,
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = int(response.status)
        except urllib.error.HTTPError as exc:
            raise MaxKBMaaSError(f"HTTP_{exc.code}:{method.upper()}:{path}") from exc
        except urllib.error.URLError as exc:
            raise MaxKBMaaSError(f"URL_ERROR:{method.upper()}:{path}") from exc
        if status < 200 or status >= 300:
            raise MaxKBMaaSError(f"HTTP_{status}:{method.upper()}:{path}")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MaxKBMaaSError(f"JSON_INVALID:{method.upper()}:{path}") from exc
        if isinstance(payload, Mapping) and "code" in payload:
            code = payload.get("code")
            if code not in (200, "200", 0, "0", None):
                raise MaxKBMaaSError(f"API_{code}")
        return payload


class MaaSClient(JsonHttpClient):
    """OpenAI-compatible probes with thinking disabled by default."""

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        extra_params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        params = dict(extra_params or {})
        params["thinking"] = {"type": "disabled"}
        payload = {"model": model, "messages": messages, **params}
        result = self.request("POST", "/chat/completions", body=payload)
        if not isinstance(result, Mapping):
            raise MaxKBMaaSError("MAAS_CHAT_RESPONSE_INVALID")
        return dict(result)

    def embed(self, *, model: str, text: str) -> dict[str, Any]:
        result = self.request(
            "POST",
            "/embeddings",
            body={"model": model, "input": [text], "encoding_format": "float"},
        )
        data = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], Mapping):
            raise MaxKBMaaSError("MAAS_EMBEDDING_RESPONSE_INVALID")
        vector = data[0].get("embedding")
        if not isinstance(vector, list) or not vector:
            raise MaxKBMaaSError("MAAS_EMBEDDING_VECTOR_MISSING")
        usage = result.get("usage") if isinstance(result, Mapping) else None
        return {
            "vector_dimension": len(vector),
            "vector_count": len(data),
            "usage_total_tokens": usage.get("total_tokens") if isinstance(usage, Mapping) else None,
        }


def _admin_client() -> JsonHttpClient:
    base = os.getenv("MAXKB_ADMIN_BASE_URL", "").strip()
    if not base:
        root_base = os.getenv("MAXKB_BASE_URL", "").strip() or DEFAULT_MAXKB_BASE_URL
        base = f"{_normalise_url(root_base)}/admin/api"
    token = os.getenv("MAXKB_ADMIN_TOKEN", "").strip()
    if not token:
        token_file = Path(
            os.getenv(
                "MAXKB_ADMIN_TOKEN_FILE",
                str(ROOT / ".local-services/maxkb_local/secrets/admin.token"),
            )
        )
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8", errors="replace").strip()
    return JsonHttpClient(base, api_key=_required(token, "MAXKB_ADMIN_TOKEN"))


def _maas_client() -> MaaSClient:
    base = _validate_maas_base_url(os.getenv("MAAS_BASE_URL", DEFAULT_MAAS_BASE_URL))
    key = _required(os.getenv("MAAS_API_KEY"), "MAAS_API_KEY")
    return MaaSClient(base, api_key=key)


class SplitProvider:
    """Route chat to DeepSeek and embeddings to Huawei MaaS."""

    def __init__(self) -> None:
        self.chat_client = MaaSClient(
            _validate_deepseek_base_url(os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)),
            api_key=_required(os.getenv(DEEPSEEK_API_KEY_ENV), DEEPSEEK_API_KEY_ENV),
        )
        self.embedding_client = _maas_client()

    def chat(self, **kwargs: Any) -> dict[str, Any]:
        return self.chat_client.chat(**kwargs)

    def embed(self, **kwargs: Any) -> dict[str, Any]:
        return self.embedding_client.embed(**kwargs)


def _model_list(admin: Any) -> list[dict[str, Any]]:
    return _list_records(admin.request("GET", "/workspace/default/model"))


def _provider_probe(provider: Any) -> dict[str, Any]:
    chat = provider.chat(
        model=DEFAULT_MAAS_LLM_MODEL,
        messages=[{"role": "user", "content": "MaxKB split-provider contract probe."}],
        extra_params={"thinking": {"type": "disabled"}},
    )
    choices = chat.get("choices") if isinstance(chat, Mapping) else None
    if not isinstance(choices, list) or not choices:
        raise MaxKBMaaSError("MAAS_CHAT_PROBE_EMPTY")

    reasoning_tokens: list[int] = []

    def collect_reasoning_tokens(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).casefold() == "reasoning_tokens":
                    try:
                        reasoning_tokens.append(int(item))
                    except (TypeError, ValueError) as exc:
                        raise MaxKBMaaSError("MAAS_CHAT_REASONING_TOKENS_INVALID") from exc
                else:
                    collect_reasoning_tokens(item)
        elif isinstance(value, list):
            for item in value:
                collect_reasoning_tokens(item)

    collect_reasoning_tokens(chat)
    if any(value > 0 for value in reasoning_tokens):
        raise MaxKBMaaSError("MAAS_CHAT_THINKING_NOT_DISABLED")
    embedding = provider.embed(
        model=DEFAULT_MAAS_EMBEDDING_MODEL,
        text="MaxKB MaaS embedding contract probe. Return one dense vector.",
    )
    actual_dimension = int(embedding.get("vector_dimension", 0))
    if actual_dimension != DEFAULT_MAAS_EMBEDDING_DIMENSION:
        raise MaxKBMaaSError(
            f"MAAS_EMBEDDING_DIMENSION_MISMATCH:expected={DEFAULT_MAAS_EMBEDDING_DIMENSION}:actual={actual_dimension}"
        )
    return {
        "status": "ready",
        "provider": "deepseek_text_maas_embedding",
        "chat": {
            "model": DEFAULT_MAAS_LLM_MODEL,
            "thinking": {"type": "disabled"},
            "reasoning_tokens": max(reasoning_tokens, default=0),
        },
        "embedding": {
            "model": DEFAULT_MAAS_EMBEDDING_MODEL,
            "expected_dimension": DEFAULT_MAAS_EMBEDDING_DIMENSION,
            "observed_dimension": actual_dimension,
            "vector_count": embedding.get("vector_count"),
        },
    }


def maxkb_generation_contract(version: str = MAXKB_PINNED_VERSION) -> dict[str, Any]:
    """Describe the native/external DeepSeek boundary for a pinned MaxKB release."""

    pinned = str(version).strip().startswith(MAXKB_PINNED_VERSION)
    if pinned:
        return {
            "version": version,
            "native_supported": False,
            "runner_mode": "external",
            "limitation": (
                "Pinned MaxKB 2.10.4 cannot persist nested thinking parameters in its "
                "native OpenAI model form; DeepSeek generation uses the external runner."
            ),
            "thinking": {"type": "disabled"},
        }
    return {
        "version": version,
        "native_supported": True,
        "runner_mode": "native",
        "limitation": "Verify nested thinking persistence against the installed MaxKB release.",
        "thinking": {"type": "disabled"},
    }


def build_external_chat_payload(
    *,
    model: str,
    messages: list[dict[str, Any]],
    extra_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the external OpenAI-compatible request with forced disabled thinking."""

    payload: dict[str, Any] = {"model": model, "messages": messages}
    payload.update(dict(extra_params or {}))
    payload["thinking"] = {"type": "disabled"}
    return payload


def verify_models(
    admin: Any,
    provider: Any | None = None,
    *,
    probe_provider: bool = True,
    llm_model_id: str | None = None,
    embedding_model_id: str | None = None,
    version: str = MAXKB_PINNED_VERSION,
) -> dict[str, Any]:
    """Verify exact MaxKB records and optionally probe both provider endpoints."""

    records = _model_list(admin)
    llm = select_model_record(
        records,
        provider=MAXKB_OPENAI_PROVIDER,
        model_type=MAXKB_LLM_TYPE,
        model_name=DEFAULT_MAAS_LLM_MODEL,
        model_id=llm_model_id,
    )
    embedding = select_model_record(
        records,
        provider=MAXKB_OPENAI_PROVIDER,
        model_type=MAXKB_EMBEDDING_TYPE,
        model_name=DEFAULT_MAAS_EMBEDDING_MODEL,
        model_id=embedding_model_id,
    )
    safe_llm = validate_model_record(
        llm,
        provider=MAXKB_OPENAI_PROVIDER,
        model_type=MAXKB_LLM_TYPE,
        model_name=DEFAULT_MAAS_LLM_MODEL,
    )
    safe_embedding = validate_model_record(
        embedding,
        provider=MAXKB_OPENAI_PROVIDER,
        model_type=MAXKB_EMBEDDING_TYPE,
        model_name=DEFAULT_MAAS_EMBEDDING_MODEL,
    )
    result: dict[str, Any] = {
        "status": "ready",
        "mutated_runtime": False,
        "provider": "deepseek_text_maas_embedding",
        "models": {"llm": safe_llm, "embedding": safe_embedding},
        "embedding_dimension": DEFAULT_MAAS_EMBEDDING_DIMENSION,
        "native_generation": maxkb_generation_contract(version),
    }
    if probe_provider:
        result["provider_probe"] = _provider_probe(provider or SplitProvider())
    else:
        result["provider_probe"] = {"status": "skipped"}
    return result


def _created_record(response: Any, payload: Mapping[str, Any]) -> dict[str, Any]:
    data: Any = response.get("data", response) if isinstance(response, Mapping) else response
    if isinstance(data, Mapping) and isinstance(data.get("model"), Mapping):
        data = data["model"]
    if not isinstance(data, Mapping):
        raise MaxKBMaaSError("MAXKB_CREATE_MODEL_RESPONSE_INVALID")
    merged = dict(payload)
    merged.update(dict(data))
    if not _model_id(merged):
        raise MaxKBMaaSError("MAXKB_CREATE_MODEL_ID_MISSING")
    return merged


def register_models(
    admin: Any,
    provider: Any | None = None,
    *,
    execute: bool = False,
    probe_provider: bool = True,
    api_key: str | None = None,
    base_url: str | None = None,
    llm_model_id: str | None = None,
    embedding_model_id: str | None = None,
    version: str = MAXKB_PINNED_VERSION,
) -> dict[str, Any]:
    """Dry-run or idempotently create the exact split-provider model records."""

    configured_embedding_key = api_key if api_key is not None else os.getenv("MAAS_API_KEY", "")
    configured_embedding_base = base_url or os.getenv("MAAS_BASE_URL", DEFAULT_MAAS_BASE_URL)
    configured_llm_key = os.getenv(DEEPSEEK_API_KEY_ENV, "")
    configured_llm_base = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL)
    if not execute:
        safe_embedding_key = configured_embedding_key if configured_embedding_key and not configured_embedding_key.startswith("<") else "<MAAS_API_KEY>"
        safe_llm_key = configured_llm_key if configured_llm_key and not configured_llm_key.startswith("<") else f"<{DEEPSEEK_API_KEY_ENV}>"
        payloads = build_registration_payloads(
            api_key=safe_embedding_key,
            base_url=configured_embedding_base,
            llm_api_key=safe_llm_key,
            llm_base_url=configured_llm_base,
        )
        return {
            "status": "dry_run",
            "mutated_runtime": False,
            "provider": "deepseek_text_maas_embedding",
            "api_key_loaded": {
                "deepseek": bool(str(configured_llm_key).strip() and not str(configured_llm_key).strip().startswith("<")),
                "maas": bool(str(configured_embedding_key).strip() and not str(configured_embedding_key).strip().startswith("<")),
            },
            "registration_payloads": _redact(payloads),
            "models": {
                "llm": {"model_name": DEFAULT_MAAS_LLM_MODEL},
                "embedding": {"model_name": DEFAULT_MAAS_EMBEDDING_MODEL, "dimension": DEFAULT_MAAS_EMBEDDING_DIMENSION},
            },
            "native_generation": maxkb_generation_contract(version),
            "operator_step_required": True,
            "operator_step": "Re-run with --execute only after reviewing the exact provider/type/model contract.",
        }

    embedding_key = _required(configured_embedding_key, "MAAS_API_KEY")
    llm_key = _required(configured_llm_key, DEEPSEEK_API_KEY_ENV)
    payloads = build_registration_payloads(
        api_key=embedding_key,
        base_url=configured_embedding_base,
        llm_api_key=llm_key,
        llm_base_url=configured_llm_base,
    )
    probe_result = {"status": "skipped"}
    if probe_provider:
        probe_result = _provider_probe(provider or SplitProvider())
    records = _model_list(admin)
    selected_models: dict[str, dict[str, Any]] = {}
    created_any = False
    specs = (
        ("llm", MAXKB_LLM_TYPE, DEFAULT_MAAS_LLM_MODEL, llm_model_id),
        ("embedding", MAXKB_EMBEDDING_TYPE, DEFAULT_MAAS_EMBEDDING_MODEL, embedding_model_id),
    )
    for key_name, model_type, model_name, explicit_id in specs:
        try:
            selected = select_model_record(
                records,
                provider=MAXKB_OPENAI_PROVIDER,
                model_type=model_type,
                model_name=model_name,
                model_id=explicit_id,
            )
            selected_models[key_name] = validate_model_record(
                selected,
                provider=MAXKB_OPENAI_PROVIDER,
                model_type=model_type,
                model_name=model_name,
            )
            continue
        except MaxKBMaaSError as exc:
            if not str(exc).startswith("MAXKB_MODEL_NOT_REGISTERED:"):
                raise

        created = _created_record(
            admin.request("POST", "/workspace/default/model", body=payloads[key_name]),
            payloads[key_name],
        )
        selected_models[key_name] = validate_model_record(
            created,
            provider=MAXKB_OPENAI_PROVIDER,
            model_type=model_type,
            model_name=model_name,
        )
        records.append(created)
        created_any = True

    return {
        "status": "registered" if created_any else "already_registered",
        "mutated_runtime": created_any,
        "provider": "deepseek_text_maas_embedding",
        "models": selected_models,
        "provider_probe": probe_result,
        "embedding_dimension": DEFAULT_MAAS_EMBEDDING_DIMENSION,
        "native_generation": maxkb_generation_contract(version),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("verify", "register"):
        command = subparsers.add_parser(name)
        command.add_argument("--skip-provider-probe", action="store_true")
        command.add_argument("--llm-model-id")
        command.add_argument("--embedding-model-id")
        command.add_argument("--version", default=MAXKB_PINNED_VERSION)
        if name == "register":
            command.add_argument("--execute", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Reuse the exact IDs recorded in .env when flags are omitted. MaxKB
    # permits duplicate provider/type/name records, so silent discovery is
    # unsafe for both verification and registration.
    llm_model_id = args.llm_model_id or os.getenv("MAXKB_LLM_MODEL_ID")
    embedding_model_id = args.embedding_model_id or os.getenv("MAXKB_EMBEDDING_MODEL_ID")
    if args.command == "verify":
        admin = _admin_client()
        result = verify_models(
            admin,
            probe_provider=not args.skip_provider_probe,
            llm_model_id=llm_model_id,
            embedding_model_id=embedding_model_id,
            version=args.version,
        )
    else:
        # A dry-run only renders local payload metadata.  Do not require a
        # running MaxKB instance or admin token until an operation can read or
        # mutate its model records.
        admin = _admin_client() if args.execute else None
        result = register_models(
            admin,
            execute=args.execute,
            probe_provider=not args.skip_provider_probe,
            llm_model_id=llm_model_id,
            embedding_model_id=embedding_model_id,
            version=args.version,
        )
    print(json.dumps(_redact(result), ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] in {"ready", "dry_run", "registered", "already_registered"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
