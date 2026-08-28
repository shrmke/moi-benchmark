#!/usr/bin/env python3
"""Audit saved HTTP requests for DeepSeek text plus MaaS embeddings.

The MaaS ``/models`` response is a catalog, not an invocation.  This audit
therefore inspects request objects separately from response objects and fails
closed on forbidden provider/model strings, non-MaaS external hosts, or image
requests.  It writes only a small summary and never copies request bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


SCHEMA = "moi-rag-bench-text-only-provider-audit-v1"
FORBIDDEN_REQUEST_RE = re.compile(
    r"(?:qwen|qianfan|taas|matrixorigin_taas|matrixorigin\.cn)",
    re.IGNORECASE,
)
VISION_CATALOG_RE = re.compile(r"qwen[^\s\"',;)}\]]*", re.IGNORECASE)
MODEL_KEYS = {
    "model",
    "model_name",
    "llm_model",
    "chat_model",
    "embedding_model",
    "agentmodel",
    "vectormodel",
}
REQUIRED_REQUEST_MODELS = frozenset({"bge-m3", "deepseek-v4-flash"})
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
ALLOWED_EXTERNAL_HOSTS = {"api.modelarts-maas.com", "api.deepseek.com"}


def _walk(value: Any) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            path = (str(key),)
            yield path, item
            for child_path, child in _walk(item):
                yield path + child_path, child
    elif isinstance(value, list):
        for index, item in enumerate(value):
            for child_path, child in _walk(item):
                yield (str(index),) + child_path, child


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _model_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold() in MODEL_KEYS and isinstance(item, str) and item.strip():
                yield item.strip()
            yield from _model_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _model_values(item)


def _has_image_payload(value: Any) -> bool:
    for path, item in _walk(value):
        if path and path[-1].casefold() == "type" and str(item).casefold() in {"image", "input_image", "video"}:
            return True
        if path and path[-1].casefold() in {"image", "image_url", "image_path", "images", "video"}:
            return True
    return False


def _violation(violations: list[dict[str, str]], path: Path, reason: str) -> None:
    violations.append({"file": path.as_posix(), "reason": reason})


def audit_runs(runs_root: Path) -> dict[str, Any]:
    runs_root = Path(runs_root).resolve()
    violations: list[dict[str, str]] = []
    request_models: set[str] = set()
    external_hosts: set[str] = set()
    catalog_vision_ids: set[str] = set()
    scanned_files = 0
    request_records = 0
    image_request_count = 0

    for path in sorted(runs_root.rglob("*.json")):
        if path.is_symlink():
            continue
        scanned_files += 1
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _violation(violations, path, f"invalid JSON artifact: {exc}")
            continue
        if not isinstance(payload, Mapping):
            continue

        operation = str(payload.get("operation") or "")
        response = payload.get("response")
        if "model" in operation.casefold() or "/models" in str((payload.get("request") or {}).get("url") or ""):
            for value in _strings(response):
                catalog_vision_ids.update(VISION_CATALOG_RE.findall(value))

        request = payload.get("request")
        if not isinstance(request, Mapping):
            continue
        request_records += 1
        request_text = json.dumps(request, ensure_ascii=False, sort_keys=True)
        if FORBIDDEN_REQUEST_RE.search(request_text):
            _violation(violations, path, "forbidden provider/model marker in request")

        url = str(request.get("url") or "")
        host = urlparse(url).hostname
        if host:
            host = host.casefold()
            if host not in LOCAL_HOSTS and host not in ALLOWED_EXTERNAL_HOSTS:
                external_hosts.add(host)
                _violation(violations, path, f"external request host outside hybrid policy: {host}")

        request_json = request.get("json")
        for model in _model_values(request_json):
            request_models.add(model)
        operation_text = f"{operation} {url}".casefold()
        if _has_image_payload(request_json) or any(token in operation_text for token in ("/images", "vision", "multimodal")):
            image_request_count += 1
            _violation(violations, path, "image/vision/multimodal request in text-only run")

    missing_required_models = sorted(REQUIRED_REQUEST_MODELS - request_models)
    unexpected_request_models = sorted(request_models - REQUIRED_REQUEST_MODELS)
    if request_records == 0:
        _violation(violations, runs_root, "no provider request records observed")
    for model in missing_required_models:
        _violation(violations, runs_root, f"required provider model not observed: {model}")
    for model in unexpected_request_models:
        _violation(violations, runs_root, f"request model outside policy: {model}")

    return {
        "schema": SCHEMA,
        "runs_root": str(runs_root),
        "scanned_json_files": scanned_files,
        "request_records": request_records,
        "request_models_observed": sorted(request_models),
        "required_request_models": sorted(REQUIRED_REQUEST_MODELS),
        "missing_required_models": missing_required_models,
        "unexpected_request_models": unexpected_request_models,
        "catalog_vision_ids_observed": sorted(catalog_vision_ids),
        "external_hosts_observed": sorted(external_hosts),
        "image_request_count": image_request_count,
        "violations": violations,
        "status": "PASS" if not violations else "FAIL",
    }


def _write_output(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    path.write_bytes(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    path.with_suffix(path.suffix + ".sha256").write_text(f"{digest}  {path.name}\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = audit_runs(args.runs_root)
    if args.output:
        _write_output(args.output, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
