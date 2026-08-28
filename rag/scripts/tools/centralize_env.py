#!/usr/bin/env python3
"""Merge local credential files into the repository-root ``.env``.

This is an explicit, local-only migration helper.  It never prints values.
Use ``--sync`` after adding a new ignored runtime credential file; add
``--strip-legacy`` once the callers have been switched to the central file.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CENTRAL_FILE = REPO_ROOT / ".env"
SOURCE_FILES = (
    "local-rag-platforms/dify-rag-eval/.env",
    "prototypes/local-matrixflow-rag/.env",
    ".local-services/providers/qianfan.env",
    ".local-services/providers/maas.env",
    ".local-services/dify_local/credentials.env",
    ".local-services/dify_local/runtime.env",
    ".local-services/fastgpt_local/fastgpt.env",
    ".local-services/maxkb_local/runtime.env",
    ".local-services/maxkb_local/credentials.env",
    ".local-services/ragflow_local/compose/runtime.env",
)
CENTRAL_PREFIXES = (
    "COMPETITOR_",
    "DEEPSEEK_",
    "DIFY_",
    "FASTGPT_",
    "MAAS_",
    "MAXKB_",
    "MINERU_",
    "MOI_",
    "OPENXML_",
    "QIANFAN_",
    "RAGFLOW_",
    "SOFFICE_",
    "TAAS_",
)
STRIP_SUFFIXES = ("_API_KEY", "_APP_KEY", "_TOKEN", "_SECRET")
ENSURE_NAMES = (
    "MOI_API_URL", "MOI_API_KEY",
    "DIFY_API_BASE_URL", "DIFY_API_KEY", "DIFY_DATASET_API_KEY",
    "DIFY_LOCAL_API_KEY", "DIFY_LOCAL_APP_ID", "DIFY_LOCAL_DATASET_API_KEY", "DIFY_LOCAL_DATASET_ID",
    "FASTGPT_BASE_URL", "FASTGPT_API_KEY", "FASTGPT_APP_ID",
    "MAXKB_BASE_URL", "MAXKB_API_KEY", "MAXKB_APPLICATION_ID", "MAXKB_APP_ID",
    "RAGFLOW_BASE_URL", "RAGFLOW_API_KEY", "RAGFLOW_CHAT_ID",
    "TAAS_BASE_URL", "TAAS_API_KEY", "QIANFAN_BASE_URL", "QIANFAN_API_KEY", "MAAS_BASE_URL", "MAAS_API_KEY",
    "MINERU_API_TOKEN", "DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY_NEW",
)


def parse_env(path: Path) -> tuple[list[str], dict[str, str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    values: dict[str, str] = {}
    for raw_line in lines:
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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return lines, values


def is_central_name(name: str) -> bool:
    return name.startswith(CENTRAL_PREFIXES)


def is_real_value(value: str) -> bool:
    lowered = value.strip().lower()
    return bool(lowered) and not any(
        marker in lowered
        for marker in ("<local-", "<baidu-", "<huawei-", "replace-with-", "your-", "<your-")
    )


def is_legacy_secret(name: str) -> bool:
    return name.endswith(STRIP_SUFFIXES)


def quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def merge_into_central(updates: dict[str, str]) -> tuple[list[str], set[str]]:
    lines, existing = parse_env(CENTRAL_FILE)
    selected: dict[str, str] = {}
    for name, value in existing.items():
        if is_central_name(name) and is_real_value(value):
            selected[name] = value
    for name, value in updates.items():
        if is_central_name(name) and is_real_value(value):
            selected.setdefault(name, value)

    output: list[str] = []
    seen: set[str] = set()
    for raw_line in lines:
        line = raw_line.strip()
        key = line.split("=", 1)[0].strip() if "=" in line and not line.startswith("#") else ""
        if key in selected:
            output.append(f"{key}={quote(selected[key])}")
            seen.add(key)
        else:
            output.append(raw_line)
    for name in sorted(set(ENSURE_NAMES) | set(selected) - set(seen)):
        if name in selected and name not in seen:
            output.append(f"{name}={quote(selected[name])}")
            seen.add(name)
        elif name not in existing:
            output.append(f"{name}=")

    CENTRAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".env.", dir=CENTRAL_FILE.parent, text=True)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
        os.replace(temporary, CENTRAL_FILE)
        CENTRAL_FILE.chmod(0o600)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return output, set(selected)


def strip_legacy_secrets(path: Path) -> list[str]:
    lines, _ = parse_env(path)
    if not lines:
        return []
    removed: list[str] = []
    output: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else ""
        if key and is_legacy_secret(key):
            removed.append(key)
            output.append(f"{key}=")
        else:
            output.append(raw_line)
    if not removed:
        return removed
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        os.fchmod(descriptor, path.stat().st_mode & 0o777)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(output).rstrip() + "\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sync", action="store_true", help="merge local ignored values into root .env")
    parser.add_argument("--strip-legacy", action="store_true", help="blank API key/token assignments in legacy files")
    args = parser.parse_args()
    if not args.sync:
        parser.error("pass --sync to modify the local root .env")

    updates: dict[str, str] = {}
    sources: list[str] = []
    for relative in SOURCE_FILES:
        path = REPO_ROOT / relative
        _, values = parse_env(path)
        found = {
            name: value
            for name, value in values.items()
            if is_central_name(name) and is_real_value(value)
        }
        if found:
            sources.append(relative)
            for name, value in found.items():
                updates.setdefault(name, value)
    _, selected = merge_into_central(updates)

    stripped: dict[str, list[str]] = {}
    if args.strip_legacy:
        for relative in SOURCE_FILES:
            path = REPO_ROOT / relative
            removed = strip_legacy_secrets(path)
            if removed:
                stripped[relative] = sorted(set(removed))

    print(json.dumps({
        "central_file": str(CENTRAL_FILE),
        "source_files_with_values": sources,
        "centralized_names": sorted(selected),
        "legacy_secret_names_blank": stripped,
    }, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
