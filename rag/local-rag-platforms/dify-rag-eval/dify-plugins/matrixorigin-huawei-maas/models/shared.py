from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from dify_plugin.errors.model import (
    InvokeAuthorizationError,
    InvokeBadRequestError,
    InvokeConnectionError,
    InvokeRateLimitError,
    InvokeServerUnavailableError,
)


DEFAULT_BASE_URL = "https://api.modelarts-maas.com/v1"
MAAS_HOST = "api.modelarts-maas.com"


def validate_base_url(value: object) -> str:
    """Return the canonical MaaS URL or fail closed before any request."""

    raw = str(value or "").strip() or DEFAULT_BASE_URL
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise InvokeBadRequestError("Huawei MaaS base_url must be the official HTTPS v1 endpoint") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != MAAS_HOST
        or port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.path.rstrip("/") != "/v1"
        or parsed.query
        or parsed.fragment
    ):
        raise InvokeBadRequestError("Huawei MaaS base_url must be the official HTTPS v1 endpoint")
    return raw.rstrip("/")


def base_url(credentials: dict) -> str:
    return validate_base_url(credentials.get("base_url") or os.getenv("MAAS_BASE_URL", DEFAULT_BASE_URL))


def headers(credentials: dict) -> dict[str, str]:
    api_key = str(credentials.get("api_key") or "").strip()
    if not api_key:
        raise InvokeAuthorizationError("Huawei MaaS API key is required")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def post_json(credentials: dict, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    response = None
    for attempt in range(4):
        try:
            response = httpx.post(
                f"{base_url(credentials)}{path}",
                headers=headers(credentials),
                json=payload,
                timeout=120,
            )
        except httpx.RequestError as exc:
            if attempt == 3:
                raise InvokeConnectionError(str(exc)) from exc
            time.sleep(1.0 * (attempt + 1))
            continue
        if response.status_code not in (429,) and response.status_code < 500:
            break
        if attempt < 3:
            time.sleep(1.0 * (attempt + 1))

    assert response is not None

    if response.status_code >= 400:
        try:
            body = response.json()
            error = body.get("error") or body.get("detail") or body.get("message") or body
            message = str(error.get("message") or error) if isinstance(error, dict) else str(error)
        except ValueError:
            message = response.text[:1000]
        if response.status_code in (401, 403):
            raise InvokeAuthorizationError(message)
        if response.status_code == 429:
            raise InvokeRateLimitError(message)
        if response.status_code >= 500:
            raise InvokeServerUnavailableError(message)
        raise InvokeBadRequestError(message)

    try:
        payload = response.json()
    except ValueError as exc:
        raise InvokeServerUnavailableError("Huawei MaaS returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise InvokeServerUnavailableError("Huawei MaaS returned a non-object JSON response")
    return payload
