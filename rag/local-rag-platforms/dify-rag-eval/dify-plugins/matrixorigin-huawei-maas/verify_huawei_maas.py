#!/usr/bin/env python3
"""Read-only readiness check for the standalone Huawei MaaS Dify package.

This command validates local package metadata and reports whether the two
operator-supplied environment values are loaded.  It never calls Huawei MaaS,
the Dify API, or a Dify database, and it never includes secret values in its
output.  Installing/configuring the provider remains an explicit Dify-console
operation because the pinned plugin-daemon management API does not provide a
safe repository-side install helper.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


DEFAULT_MAAS_BASE_URL = "https://api.modelarts-maas.com/v1"
MAAS_HOST = "api.modelarts-maas.com"
MAAS_LLM_MODEL = "glm-5.2"
MAAS_EMBEDDING_MODEL = "bge-m3"
MAAS_EMBEDDING_DIMENSION = 1024
PLUGIN_ID = "matrixorigin/matrixorigin_huawei_maas"
PROVIDER_ID = f"{PLUGIN_ID}/huawei_maas"
PACKAGE_NAME = "matrixorigin-huawei-maas.difypkg"


class ContractError(RuntimeError):
    """A package or local configuration contract is not ready."""


def _normalise_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _validate_base_url(value: str) -> str:
    raw = _normalise_url(value) or DEFAULT_MAAS_BASE_URL
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ContractError("MAAS_BASE_URL_INVALID") from exc
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
        raise ContractError("MAAS_BASE_URL_INVALID")
    return raw


def _loaded_secret(name: str) -> bool:
    value = os.getenv(name, "").strip()
    return bool(value and not value.startswith("<"))


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ContractError(f"SOURCE_FILE_UNREADABLE:{path.name}") from exc


def _require_text(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise ContractError(f"{label}_MISSING")


def _source_contract(root: Path) -> dict[str, Any]:
    manifest = _text(root / "manifest.yaml")
    provider = _text(root / "provider/huawei_maas.yaml")
    llm_position = _text(root / "models/llm/_position.yaml")
    llm = _text(root / "models/llm/glm-5.2.yaml")
    embedding = _text(root / "models/text_embedding/bge-m3.yaml")

    required = (
        (manifest, "version: 0.0.1", "MANIFEST_VERSION"),
        (manifest, "name: matrixorigin_huawei_maas", "MANIFEST_NAME"),
        (manifest, "provider/huawei_maas.yaml", "MANIFEST_PROVIDER"),
        (provider, "provider: huawei_maas", "PROVIDER_NAME"),
        (provider, f"default: {DEFAULT_MAAS_BASE_URL}", "PROVIDER_BASE_URL"),
        (llm_position, f"- {MAAS_LLM_MODEL}", "LLM_POSITION"),
        (llm, f"model: {MAAS_LLM_MODEL}", "LLM_MODEL"),
        (embedding, f"model: {MAAS_EMBEDDING_MODEL}", "EMBEDDING_MODEL"),
    )
    for content, needle, label in required:
        _require_text(content, needle, label)

    # This is a text-only package by contract.  Keep the check explicit so a
    # future model glob cannot silently reintroduce image/multimodal models.
    for content, label in ((manifest, "MANIFEST"), (provider, "PROVIDER")):
        if re.search(r"(?i)qwen|multimodal|vision|image", content):
            raise ContractError(f"{label}_TEXT_ONLY_CONTRACT_FAILED")

    # Dify 1.16.1 rejects a custom ``dimension`` key in text-embedding model
    # YAML.  The effective width is therefore frozen in this verifier and
    # checked by the runtime MaaS embedding probe, rather than duplicated in
    # plugin model metadata.

    return {
        "version": "0.0.1",
        "plugin_id": PLUGIN_ID,
        "provider_id": PROVIDER_ID,
        "models": {
            "llm": MAAS_LLM_MODEL,
            "text_embedding": MAAS_EMBEDDING_MODEL,
            "embedding_dimension": MAAS_EMBEDDING_DIMENSION,
        },
    }


def _archive_contract(package_path: Path) -> dict[str, Any]:
    if not package_path.is_file():
        return {"path": str(package_path), "present": False, "transient_files": []}

    try:
        with zipfile.ZipFile(package_path) as package:
            names = package.namelist()
            transient = sorted(
                name
                for name in names
                if ".DS_Store" in name
                or "__pycache__/" in name
                or name.endswith(".pyc")
            )
            if transient:
                raise ContractError("PACKAGE_TRANSIENT_FILES_PRESENT")
            required_names = {
                "manifest.yaml",
                "provider/huawei_maas.yaml",
                "models/llm/glm-5.2.yaml",
                "models/text_embedding/bge-m3.yaml",
            }
            missing = sorted(required_names.difference(names))
            if missing:
                raise ContractError(f"PACKAGE_FILES_MISSING:{','.join(missing)}")
            manifest = package.read("manifest.yaml").decode("utf-8")
            _require_text(manifest, "version: 0.0.1", "PACKAGE_MANIFEST_VERSION")
            _require_text(manifest, "name: matrixorigin_huawei_maas", "PACKAGE_MANIFEST_NAME")
    except zipfile.BadZipFile as exc:
        raise ContractError("PACKAGE_INVALID_ZIP") from exc

    return {"path": str(package_path), "present": True, "transient_files": []}


def verify_contract(
    *,
    package_path: str | Path | None = None,
    source_root: str | Path | None = None,
) -> dict[str, Any]:
    """Return a secret-free local readiness report without mutating anything."""

    root = Path(source_root) if source_root else Path(__file__).resolve().parent
    package = Path(package_path) if package_path else root.parent / PACKAGE_NAME
    errors: list[str] = []
    source: dict[str, Any]
    try:
        source = _source_contract(root)
    except ContractError as exc:
        source = {"plugin_id": PLUGIN_ID, "provider_id": PROVIDER_ID}
        errors.append(str(exc))

    try:
        archive = _archive_contract(package)
    except ContractError as exc:
        archive = {"path": str(package), "present": package.is_file(), "transient_files": []}
        errors.append(str(exc))

    base_url = _normalise_url(os.getenv("MAAS_BASE_URL", DEFAULT_MAAS_BASE_URL)) or DEFAULT_MAAS_BASE_URL
    try:
        base_url = _validate_base_url(base_url)
    except ContractError as exc:
        errors.append(str(exc))
    key_loaded = _loaded_secret("MAAS_API_KEY")
    report = {
        "status": "ready" if not errors and archive.get("present") and key_loaded else "blocked",
        "mutated_runtime": False,
        "plugin_id": PLUGIN_ID,
        "provider_id": PROVIDER_ID,
        "maas": {"base_url": base_url, "api_key_loaded": key_loaded},
        "models": {
            "llm": MAAS_LLM_MODEL,
            "text_embedding": MAAS_EMBEDDING_MODEL,
            "embedding_dimension": MAAS_EMBEDDING_DIMENSION,
        },
        "source": source,
        "package": archive,
        "operator_step_required": True,
        "operator_step": (
            "Install the archive through Dify Plugins > Install Plugin > Via Local File, "
            "then configure Huawei Cloud MaaS in Settings > Model Provider with the "
            "MAAS_API_KEY secret and verify glm-5.2 and bge-m3 are listed."
        ),
        "errors": errors,
    }
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, help="path to matrixorigin-huawei-maas.difypkg")
    parser.add_argument("--source-root", type=Path, help="standalone plugin source directory")
    parser.add_argument("--json", action="store_true", help="emit formatted JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = verify_contract(package_path=args.package, source_root=args.source_root)
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
