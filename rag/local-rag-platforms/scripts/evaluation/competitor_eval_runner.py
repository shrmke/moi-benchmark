#!/usr/bin/env python3
"""Safe, resumable evaluation runner for the four local RAG platforms.

The runner consumes a ``competitor-eval-ready-v1`` package and exposes four
stages: ``preflight``, ``ingest``, ``retrieval``, ``qa`` and ``all``.  It is
deliberately conservative at the product boundary:

* the immutable start record and initial denominator are written before an
  adapter is allowed to make an HTTP request;
* every request is sent through the existing ArtifactHTTP implementation from
  ``mmdocir_competitor_eval.py``;
* resource-map and terminal-ledger writes are checkpoints, so an interrupted
  run can resume without silently deleting questions or rebuilding completed
  resources;
* unsupported media and unsupported product contracts are terminal records,
  not filtered-out rows; and
* text generation is frozen to DeepSeek ``deepseek-v4-flash`` using the
  dedicated ``DEEPSEEK_API_KEY_NEW`` credential, while embeddings remain
  frozen to Huawei MaaS ``bge-m3``; and
* Qianfan, TaaS, MatrixOrigin, and cross-provider credential fallbacks are
  rejected before any request.

The module is intentionally dependency-free beyond the Python standard
library and the existing local helper module.  Tests replace ArtifactHTTP;
the runner itself never needs a live service for ``--dry-run`` or package
preflight validation.
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import shlex
import stat
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit


HERE = Path(__file__).resolve().parent
PLATFORM_ROOT = HERE.parents[1]
ROOT = PLATFORM_ROOT.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:
    # These are the shared helpers requested by the local benchmark contract.
    import mmdocir_competitor_eval as _shared_helpers  # type: ignore
    from mmdocir_competitor_eval import (  # type: ignore
        ArtifactHTTP,
        DifyNativeDatasetBinding,
        EvalError,
        HTTPFailure,
        ProviderFailure,
        ProviderUnavailable,
        answer_from,
        first_value,
        json_dump,
        list_items,
        normalize,
        parse_dotenv,
        redact,
        redact_text,
        sha256_bytes,
        utc_now,
        value_from,
    )
except ImportError as exc:  # pragma: no cover - only useful for a damaged checkout
    raise RuntimeError("mmdocir_competitor_eval.py is required by the shared runner") from exc


DifyAppBinding = DifyNativeDatasetBinding


CONTRACT_PATH = HERE / "competitor_eval_platform_contracts.json"
CONTRACTS: dict[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
PACKAGE_SCHEMA = str(CONTRACTS["package_schema"])
SYSTEMS = ("moi_local", "dify_local", "fastgpt_local", "maxkb_local")
STAGES = ("preflight", "ingest", "retrieval", "qa", "all")
GLOBAL_RESOURCE = "__global__"
DEFAULT_TOP_K = [1, 3, 5, 10]
THINKING_CONFIG = dict(CONTRACTS["defaults"].get("thinking", {"type": "disabled"}))
SHARED_PROMPT_VERSION = "competitor-eval-shared-rag-v1"
SHARED_PROMPT_TEXT = (
    "Answer the question using only the supplied retrieved context. "
    "If the context is insufficient, say so briefly. Keep the answer concise."
)
SHARED_PROMPT_HASH = f"sha256:{hashlib.sha256(SHARED_PROMPT_TEXT.encode('utf-8')).hexdigest()}"
DEFAULT_MAX_OUTPUT_TOKENS = 1024
MEDIA_ALIASES = {
    "txt": "text",
    "text/plain": "text",
    "md": "markdown",
    "text/markdown": "markdown",
    "application/pdf": "pdf",
    "jpg": "image",
    "jpeg": "image",
    "png": "image",
    "webp": "image",
    "gif": "image",
    "csv": "table",
    "audio/mpeg": "audio",
    "mp3": "audio",
}
TERMINAL_STATUSES = {
    "SUCCESS",
    "EMPTY",
    "TIMEOUT",
    "FAILED",
    "BLOCKED",
    "UNSUPPORTED",
    "INTERRUPTED",
}
# Reject the legacy MatrixOrigin TaaS channel and endpoint, while allowing
# the separately named Huawei Cloud MaaS plugin owned by MatrixOrigin.
# Retired/non-MaaS provider or model markers.  Keep the parser-compatible
# legacy CLI fields, but never allow these values to become an active profile.
SECRET_WORDS = (
    "taas",
    "matrixorigin_taas",
    "matrixorigin.cn",
    "qianfan",
    "baidubce",
    "qwen",
)
MAAS_HOST = "api.modelarts-maas.com"
DEEPSEEK_HOST = "api.deepseek.com"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_LLM_MODEL = "deepseek-v4-flash"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY_NEW"
DIFY_GENERIC_OAI_ADAPTER = "matrixorigin/matrixorigin_taas/matrixorigin_taas"
# Dify's public dataset hit-testing payload validates ``query`` at 250
# characters.  Keep the original benchmark question in the terminal ledger,
# but make the wire-level truncation explicit and auditable.
DIFY_RETRIEVAL_QUERY_MAX_CHARS = 250
# Reasoning models can spend the first 32 tokens entirely in
# ``reasoning_content`` and legitimately return an empty final answer.  Keep
# the provider smoke large enough to observe the final completion token.
PROVIDER_CHAT_PROBE_MAX_TOKENS = 256
FASTGPT_MAAS_QA_MIN_INTERVAL_DEFAULT = 1.1
MAXKB_LEGACY_QA_MIN_INTERVAL_DEFAULT = 1.1  # compatibility name; native MaxKB QA is unreachable
# MaxKB's OpenAI-compatible endpoint may front an external model even when
# the runner's own text provider is MaaS.  The observed provider quota is
# 15 requests/minute, so keep a conservative default for the native QA path.
MAXKB_QA_MIN_INTERVAL_DEFAULT = 4.1
MAXKB_MAAS_PROVIDER = "model_openai_provider"
MAXKB_EXTERNAL_QA_CONTRACT = "external_generation_from_maxkb_retrieval"
MAXKB_QA_COMPARABILITY = "diagnostic_admin_retrieval_plus_external_deepseek_generation"
# MaxKB's diagnostic hit-test returns the full paragraph plus all of its
# internal chunks. Sending five full documents to MaaS can exceed the model
# gateway's content-safety/context boundary even though the native MaxKB
# endpoint would normally select a much smaller context. Use ranked chunks for
# the external-generation compatibility path and keep the wire budget bounded.
MAXKB_EXTERNAL_CONTEXT_CHARS = 16000
MAXKB_EXTERNAL_MAX_CHUNKS = 64
DIFY_MAX_INDEXING_SCOPES_DEFAULT = 3
DIFY_MAX_INDEXING_SCOPES_LIMIT = 16
QA_CONCURRENCY_DEFAULT = 4
QA_CONCURRENCY_LIMIT = 16
MAXKB_INGEST_CONCURRENCY_DEFAULT = 2
MAXKB_INGEST_CONCURRENCY_LIMIT = 16
FASTGPT_INGEST_CONCURRENCY_DEFAULT = 2
FASTGPT_INGEST_CONCURRENCY_LIMIT = 16
MAXKB_INGEST_RETRY_LIMIT = 3
MAXKB_INGEST_RETRY_BACKOFF_SECONDS = 0.5
MAXKB_INGEST_RETRY_BACKOFF_MAX_SECONDS = 8.0
# MaxKB rejects a single paragraph.content above 102400 characters. Keep a
# safety margin for server-side normalization and split at the adapter edge.
MAXKB_PARAGRAPH_MAX_CHARS = 90000
MAXKB_INDEX_REFRESH_LIMIT = 3
MAXKB_TASK_BUSY_MARKERS = (
    "任务正在执行",
    "任务执行中",
    "task is executing",
    "task executing",
    "task is running",
    "task running",
    "in progress",
    "正在处理",
    "processing",
)


def _atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write a file durably, then atomically replace its destination."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def json_dump(path: Path, payload: Any) -> None:
    """Persist redacted JSON and its hash without exposing partial JSON."""

    path = Path(path)
    encoded = (
        json.dumps(redact(payload), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    _atomic_write(path, encoded, mode=mode)
    sidecar = path.with_suffix(path.suffix + ".sha256")
    try:
        sidecar_mode = stat.S_IMODE(sidecar.stat().st_mode)
    except FileNotFoundError:
        sidecar_mode = None
    _atomic_write(
        sidecar,
        f"{sha256_bytes(encoded)}  {path.name}\n".encode("utf-8"),
        mode=sidecar_mode,
    )
    _fsync_directory(path.parent)


# ArtifactHTTP resolves ``json_dump`` through the shared module's globals.
# Bind the runner's durable implementation there too, so raw HTTP artifacts
# receive the same atomic-write guarantee as resource-map checkpoints.
_shared_helpers.json_dump = json_dump


class RunnerError(RuntimeError):
    """A safe, expected runner failure that belongs in an artifact."""


class PackageError(RunnerError):
    pass


class ContractUnsupported(RunnerError):
    pass


def _now_run_id(system: str, dataset: str) -> str:
    return f"{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{system}-{_safe_name(dataset, 48)}"


def _safe_name(value: Any, limit: int = 100) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._") or "item"
    return cleaned[:limit]


def _bounded_unique_name(value: Any, limit: int) -> str:
    """Keep API resource names bounded without truncating away uniqueness."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._") or "item"
    if len(cleaned) <= limit:
        return cleaned
    digest = hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]
    return f"{cleaned[: limit - len(digest) - 1]}-{digest}"


def _moi_identifier(value: Any, limit: int = 48) -> str:
    """Return a stable SQL identifier component unique to this run/resource."""

    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", str(value)).strip("_") or "run"
    if cleaned[0].isdigit():
        cleaned = f"r_{cleaned}"
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:10]
    prefix_limit = max(1, limit - len(digest) - 1)
    return f"{cleaned[:prefix_limit]}_{digest}"


def _enabled(value: Any) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def _dify_max_indexing_scopes(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Resolve the bounded document-local Dify submission window.

    The CLI value is intentionally preferred over the environment so a
    resumed run can be explicitly throttled by its invocation.  Sixteen is
    the safety ceiling after the batched MaaS embedding path reduced external
    pressure; the default remains three scopes in flight.
    """

    raw = getattr(args, "dify_max_indexing_scopes", None)
    if raw is None:
        raw = value_from(
            env,
            "DIFY_MAX_INDEXING_SCOPES",
            "COMPETITOR_DIFY_MAX_INDEXING_SCOPES",
            default=str(DIFY_MAX_INDEXING_SCOPES_DEFAULT),
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RunnerError(f"DIFY_MAX_INDEXING_SCOPES_INVALID:{raw}") from exc
    if not 1 <= value <= DIFY_MAX_INDEXING_SCOPES_LIMIT:
        raise RunnerError(
            f"DIFY_MAX_INDEXING_SCOPES_INVALID:{value}:expected 1..{DIFY_MAX_INDEXING_SCOPES_LIMIT}"
        )
    return value


def _qa_concurrency(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Resolve the bounded QA worker count, preferring an explicit CLI value."""

    raw = getattr(args, "qa_concurrency", None)
    if raw is None:
        raw = value_from(
            env,
            "COMPETITOR_EVAL_QA_CONCURRENCY",
            "COMPETITOR_QA_CONCURRENCY",
            "COMPETITOR_EVAL_CONCURRENCY",
            "COMPETITOR_CONCURRENCY",
            "QA_CONCURRENCY",
            default=str(QA_CONCURRENCY_DEFAULT),
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RunnerError(f"QA_CONCURRENCY_INVALID:{raw}") from exc
    if not 1 <= value <= QA_CONCURRENCY_LIMIT:
        raise RunnerError(
            f"QA_CONCURRENCY_INVALID:{value}:expected 1..{QA_CONCURRENCY_LIMIT}"
        )
    return value


def _maxkb_ingest_concurrency(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Resolve the bounded MaxKB resource worker count."""

    raw = getattr(args, "maxkb_ingest_concurrency", None)
    if raw is None:
        raw = value_from(
            env,
            "MAXKB_INGEST_CONCURRENCY",
            "COMPETITOR_MAXKB_INGEST_CONCURRENCY",
            "COMPETITOR_EVAL_MAXKB_INGEST_CONCURRENCY",
            "MAXKB_CONCURRENCY",
            default=str(MAXKB_INGEST_CONCURRENCY_DEFAULT),
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RunnerError(f"MAXKB_INGEST_CONCURRENCY_INVALID:{raw}") from exc
    if not 1 <= value <= MAXKB_INGEST_CONCURRENCY_LIMIT:
        raise RunnerError(
            f"MAXKB_INGEST_CONCURRENCY_INVALID:{value}:expected 1..{MAXKB_INGEST_CONCURRENCY_LIMIT}"
        )
    return value


def _fastgpt_ingest_concurrency(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """Resolve the bounded FastGPT resource worker count."""

    raw = getattr(args, "fastgpt_ingest_concurrency", None)
    if raw is None:
        raw = value_from(
            env,
            "FASTGPT_INGEST_CONCURRENCY",
            "COMPETITOR_FASTGPT_INGEST_CONCURRENCY",
            "COMPETITOR_EVAL_FASTGPT_INGEST_CONCURRENCY",
            "FASTGPT_CONCURRENCY",
            default=str(FASTGPT_INGEST_CONCURRENCY_DEFAULT),
        )
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RunnerError(f"FASTGPT_INGEST_CONCURRENCY_INVALID:{raw}") from exc
    if not 1 <= value <= FASTGPT_INGEST_CONCURRENCY_LIMIT:
        raise RunnerError(
            f"FASTGPT_INGEST_CONCURRENCY_INVALID:{value}:expected 1..{FASTGPT_INGEST_CONCURRENCY_LIMIT}"
        )
    return value


def _maxkb_retryable_payload(payload: Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    code = first_value(payload, ("code", "status_code", "http_status"))
    try:
        numeric_code = int(code) if code not in (None, "") else None
    except (TypeError, ValueError):
        numeric_code = None
    if numeric_code not in (None, 0, 200) and (numeric_code == 429 or numeric_code >= 500):
        return True
    if code in (None, "", 0, "0", 200, "200"):
        return False
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True).casefold()
    return any(marker.casefold() in text for marker in MAXKB_TASK_BUSY_MARKERS)


def _maxkb_retryable_exception(exc: BaseException) -> bool:
    status = getattr(exc, "status", None)
    try:
        numeric_status = int(status) if status not in (None, "") else None
    except (TypeError, ValueError):
        numeric_status = None
    if numeric_status == 429 or (numeric_status is not None and 500 <= numeric_status <= 599):
        return True
    body = getattr(exc, "body", None)
    text = f"{exc} {body or ''}".casefold()
    if re.search(r"\b(?:http(?: error)?\s*)?(?:429|5\d{2})\b", text):
        return True
    return any(marker.casefold() in text for marker in MAXKB_TASK_BUSY_MARKERS)


def _maxkb_retry_delay(attempt: int) -> float:
    return min(
        MAXKB_INGEST_RETRY_BACKOFF_MAX_SECONDS,
        MAXKB_INGEST_RETRY_BACKOFF_SECONDS * (2 ** max(0, attempt)),
    )


def _native_setup_status(exc: BaseException) -> str:
    text = str(exc).casefold()
    if isinstance(exc, ContractUnsupported) or any(marker in text for marker in ("http_404", "http 404", "http_405", "http 405", "unsupported")):
        return "unsupported"
    return "blocked"


def _build_fastgpt_isolated_app_payload(
    *,
    provider_name: str,
    dataset_id: str,
    dataset_name: str,
    llm_model: str,
    embedding_model: str,
) -> dict[str, Any]:
    """Load the working FastGPT payload helper under a collision-proof name."""

    helper_path = PLATFORM_ROOT / "fastgpt_local" / "fastgpt_local.py"
    spec = importlib.util.spec_from_file_location("_competitor_eval_fastgpt_payload_helper", helper_path)
    if spec is None or spec.loader is None:
        raise ContractUnsupported("FASTGPT_ISOLATED_APP_PAYLOAD_HELPER_UNAVAILABLE")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    builder = getattr(module, "build_isolated_app_payload", None)
    if not callable(builder):
        raise ContractUnsupported("FASTGPT_ISOLATED_APP_PAYLOAD_BUILDER_MISSING")
    return builder(
        provider_name=provider_name,
        dataset_id=dataset_id,
        dataset_name=dataset_name,
        llm_model=llm_model,
        embedding_model=embedding_model,
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha256_canonical_records(values: Iterable[Any]) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(_canonical_bytes(value))
    return digest.hexdigest()


def _write_hash(path: Path) -> None:
    data = path.read_bytes()
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{sha256_bytes(data)}  {path.name}\n", encoding="utf-8"
    )


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]], *, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with path.open(mode, encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(redact(dict(row)), ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _write_hash(path)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def _load_json_or_jsonl(path: Path) -> Any:
    if not path.exists():
        raise PackageError(f"PACKAGE_FILE_MISSING: {path}")
    if path.suffix.lower() == ".jsonl":
        rows: list[Any] = []
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if line.strip():
                        rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise PackageError(f"PACKAGE_JSONL_INVALID: {path}:{line_number}: {exc}") from exc
        return rows
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise PackageError(f"PACKAGE_JSON_INVALID: {path}: {exc}") from exc


def _as_records(value: Any, root: Path, label: str) -> tuple[list[Any], Path | None]:
    """Resolve an inline list or a JSON/JSONL path used by a package manifest."""

    source: Path | None = None
    if isinstance(value, (str, Path)):
        source = (root / value).resolve()
        if source.is_dir() and label == "documents":
            value = [str(path.relative_to(root)) for path in sorted(source.rglob("*")) if path.is_file()]
        else:
            value = _load_json_or_jsonl(source)
    elif isinstance(value, dict) and "path" in value:
        source = (root / str(value["path"])).resolve()
        if source.is_dir() and label == "documents":
            value = [str(path.relative_to(root)) for path in sorted(source.rglob("*")) if path.is_file()]
        else:
            value = _load_json_or_jsonl(source)
    if isinstance(value, dict):
        for key in ("items", "records", "documents", "questions", "data"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list):
        raise PackageError(f"PACKAGE_{label.upper()}_MUST_BE_LIST")
    return list(value), source


def _iter_jsonl(path: Path) -> Iterable[Any]:
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle, start=1):
                if line.strip():
                    yield json.loads(line)
    except json.JSONDecodeError as exc:
        raise PackageError(f"PACKAGE_JSONL_INVALID: {path}:{line_number}: {exc}") from exc


def _iter_records(value: Any, root: Path, label: str) -> tuple[Iterable[Any], Path | None]:
    source: Path | None = None
    path_value: Any = value
    if isinstance(value, Mapping) and "path" in value:
        path_value = value["path"]
    if isinstance(path_value, (str, Path)):
        source = (root / path_value).resolve()
        if source.suffix.casefold() == ".jsonl":
            if not source.is_file():
                raise PackageError(f"PACKAGE_FILE_MISSING: {source}")
            return _iter_jsonl(source), source
    records, source = _as_records(value, root, label)
    return iter(records), source


def _compact_question_raw(item: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "gold", "evidence", "gold_document_ids", "gold_doc_ids", "relevant_document_ids",
        "relevant_documents", "gold_documents", "gold_evidence", "question_type", "answerable", "metadata",
    )
    return {key: item[key] for key in keys if key in item}


def _artifact_path(
    manifest: Mapping[str, Any],
    raw_manifest: Mapping[str, Any],
    root: Path,
    names: Iterable[str],
) -> Path | None:
    """Resolve an artifact entry without imposing a package-local copy layout.

    Ready-v1 packages describe their inputs under ``artifacts``.  The artifact
    value may be a path string or an object containing ``path``; accepting both
    keeps the loader compatible with the earlier manifest form as well.
    """

    wanted = tuple(names)
    for container in (manifest, raw_manifest):
        artifacts = container.get("artifacts")
        if not isinstance(artifacts, Mapping):
            continue
        for name in wanted:
            value = artifacts.get(name)
            if value is None and not name.endswith(".jsonl"):
                value = artifacts.get(f"{name}.jsonl")
            if isinstance(value, Mapping):
                value = value.get("path")
            if value not in (None, ""):
                candidate = Path(str(value)).expanduser()
                return (candidate if candidate.is_absolute() else root / candidate).resolve()
    return None


def _split_fastgpt_text(text: str, max_chars: int) -> list[str]:
    """Bound a FastGPT pre-chunked item without destroying paragraph locality."""
    value = text.strip()
    if not value:
        return []
    if len(value) <= max_chars:
        return [value]
    parts: list[str] = []
    remaining = value
    while len(remaining) > max_chars:
        cut = remaining.rfind("\n", 0, max_chars + 1)
        if cut < max_chars // 2:
            cut = max_chars
        part = remaining[:cut].strip()
        if part:
            parts.append(part)
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


def _split_maxkb_text(text: str, max_chars: int = MAXKB_PARAGRAPH_MAX_CHARS) -> list[str]:
    """Split MaxKB input without dropping evidence or sending NUL bytes."""
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    return _split_fastgpt_text(str(text).replace("\x00", ""), max_chars)


def _fastgpt_push_items(
    content: str,
    document: Document,
    max_chars: int,
    *,
    group_markers: bool = False,
) -> list[dict[str, Any]]:
    """Turn a materialized candidate file into bounded searchable items.

    Ready-v1 candidate markdown contains one marker per source page. Keeping
    those marker bodies as separate items preserves the benchmark's page
    boundary while preventing a whole document from becoming one giant quote.
    Ordinary parsed-text/PDF artifacts use the same character bound as a
    conservative fallback.
    """
    marker_pattern = re.compile(r"^<!-- competitor-eval-candidate:(?P<meta>\{.*\}) -->\s*$", re.MULTILINE)
    matches = list(marker_pattern.finditer(content))
    segments: list[tuple[str, dict[str, Any]]] = []
    if matches:
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
            try:
                marker = json.loads(match.group("meta"))
            except json.JSONDecodeError:
                marker = {}
            segments.append((content[match.end():end], marker if isinstance(marker, dict) else {}))
    else:
        segments.append((content, {}))

    items: list[dict[str, Any]] = []
    chunk_index = 0
    if group_markers and matches:
        grouped: list[tuple[str, list[dict[str, Any]]]] = []
        current_parts: list[str] = []
        current_markers: list[dict[str, Any]] = []
        current_chars = 0

        def flush_group() -> None:
            nonlocal current_parts, current_markers, current_chars
            if current_parts:
                grouped.append(("\n\n".join(current_parts), list(current_markers)))
            current_parts = []
            current_markers = []
            current_chars = 0

        for segment_text, marker in segments:
            for text in _split_fastgpt_text(segment_text, max_chars):
                extra = len(text) + (2 if current_parts else 0)
                if current_parts and current_chars + extra > max_chars:
                    flush_group()
                current_parts.append(text)
                current_chars += len(text) + (2 if len(current_parts) > 1 else 0)
                if marker:
                    current_markers.append(marker)
        flush_group()
        for text, markers in grouped:
            items.append({
                "q": text,
                "chunkIndex": chunk_index,
                "metadata": {
                    "document_id": document.document_id,
                    "media": document.media,
                    "chunk_source": "candidate_marker_grouped",
                    "candidate_count": len(markers),
                    "candidates": markers,
                },
            })
            chunk_index += 1
        return items

    for segment_text, marker in segments:
        for segment_index, text in enumerate(_split_fastgpt_text(segment_text, max_chars)):
            metadata: dict[str, Any] = {
                "document_id": document.document_id,
                "media": document.media,
                "chunk_source": "candidate_marker" if marker else "bounded_artifact",
                "segment_index": segment_index,
            }
            if marker:
                metadata["candidate"] = marker
            items.append({"q": text, "chunkIndex": chunk_index, "metadata": metadata})
            chunk_index += 1
    return items


def _manifest_path(package: Path) -> tuple[Path, Path]:
    resolved = package.expanduser().resolve()
    if resolved.is_file():
        return resolved.parent, resolved
    if not resolved.is_dir():
        raise PackageError(f"PACKAGE_NOT_FOUND: {package}")
    for name in ("package.json", "manifest.json", "competitor-eval-ready.json", "package-manifest.json"):
        candidate = resolved / name
        if candidate.exists():
            return resolved, candidate
    raise PackageError(
        f"PACKAGE_MANIFEST_MISSING: expected package.json, manifest.json, competitor-eval-ready.json, or package-manifest.json in {resolved}"
    )


def _validate_declared_counts(
    manifest: Mapping[str, Any],
    actual: Mapping[str, int],
    *,
    error_prefix: str = "PACKAGE",
) -> None:
    """Check manifest counts when a package declares them.

    Older ready packages intentionally omit some counts, so this is a
    conditional admission check rather than a new requirement for every
    historical package.  The derived MOI text-only package declares all three
    counts and therefore cannot silently drift between build and execution.
    """

    declared = manifest.get("counts")
    if not isinstance(declared, Mapping):
        return
    for key, observed in actual.items():
        if key not in declared:
            continue
        value = declared[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise PackageError(f"{error_prefix}_DECLARED_COUNT_INVALID:{key}")
        if value != observed:
            raise PackageError(
                f"{error_prefix}_COUNT_MISMATCH:{key}:declared={value}:actual={observed}"
            )


def _normal_media(value: Any, path: Path | None = None) -> list[str]:
    raw: list[Any]
    if value is None:
        raw = []
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = [value]
    result: list[str] = []
    for item in raw:
        text = str(item).strip().lower()
        if not text:
            continue
        text = MEDIA_ALIASES.get(text, text)
        if text.startswith("image/"):
            text = "image"
        if "/" in text and text in MEDIA_ALIASES:
            text = MEDIA_ALIASES[text]
        if text not in result:
            result.append(text)
    if not result and path is not None:
        suffix = path.suffix.lower().lstrip(".")
        result.append(MEDIA_ALIASES.get(suffix, suffix or "text"))
    return result or ["text"]


_MEDIA_TUPLES: dict[tuple[str, ...], tuple[str, ...]] = {}


def _media_tuple(value: Any, path: Path | None = None) -> tuple[str, ...]:
    normalized = tuple(_normal_media(value, path))
    return _MEDIA_TUPLES.setdefault(normalized, normalized)


def _ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int)):
        return [str(value)]
    if isinstance(value, Mapping):
        for key in ("id", "document_id", "file_id", "path", "image_path", "value"):
            if value.get(key) not in (None, ""):
                return [str(value[key])]
        return []
    if isinstance(value, (list, tuple, set)):
        result: list[str] = []
        for item in value:
            result.extend(_ids(item))
        return list(dict.fromkeys(result))
    return []


class Document:
    __slots__ = (
        "document_id", "scope_id", "ingest_role", "package_path", "content", "media",
        "declared_hash", "marker_page", "marker_layout", "marker_quote", "pages", "artifact_path",
    )

    def __init__(
        self,
        *,
        document_id: str,
        scope_id: str,
        ingest_role: str,
        package_path: Path | None,
        content: str | None,
        media: tuple[str, ...],
        declared_hash: str,
        marker_page: Any = None,
        marker_layout: Any = None,
        marker_quote: Any = None,
        pages: int = 0,
        artifact_path: Path | None = None,
    ) -> None:
        self.document_id = document_id
        self.scope_id = scope_id
        self.ingest_role = ingest_role
        self.package_path = package_path
        self.content = content
        self.media = media
        self.declared_hash = declared_hash
        self.marker_page = marker_page
        self.marker_layout = marker_layout
        self.marker_quote = marker_quote
        self.pages = pages
        self.artifact_path = artifact_path

    @property
    def raw(self) -> dict[str, Any]:
        metadata = {
            key: value
            for key, value in {
                "page_number": self.marker_page,
                "layout": self.marker_layout,
                "quote": self.marker_quote,
                "pages": self.pages or None,
            }.items()
            if value not in (None, "")
        }
        return {
            "doc_id": self.document_id,
            "scope_id": self.scope_id,
            "ingest_role": self.ingest_role,
            "sha256": self.declared_hash,
            "metadata": metadata,
        }

    @property
    def source_hash(self) -> str:
        declared = self.declared_hash
        if declared not in (None, ""):
            return str(declared).removeprefix("sha256:")
        if self.package_path is not None and self.package_path.exists():
            return _sha256_file(self.package_path)
        return sha256_bytes((self.content or "").encode("utf-8"))


@dataclass
class Question:
    question_id: str
    text: str
    document_ids: list[str]
    scope_ids: list[str]
    answer: str
    media: list[str]
    image_paths: list[Path]
    raw: dict[str, Any]

    @property
    def gold(self) -> Any:
        if self.raw.get("gold") is not None:
            return self.raw["gold"]
        if self.raw.get("evidence") is not None:
            return self.raw["evidence"]
        return {
            "document_ids": self.raw.get(
                "gold_document_ids",
                self.raw.get("gold_doc_ids", self.raw.get("relevant_document_ids", [])),
            ),
            "evidence": self.raw.get("gold_evidence", []),
        }


@dataclass
class EvalPackage:
    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    dataset: str
    revision: str
    split: str
    scope: str
    protocol_tag: str
    condition: str
    documents: list[Document]
    questions: list[Question]
    scope_groups: dict[str, list[Document]]
    documents_by_id: dict[str, Document]
    ingest_representation: str
    document_source: Path | None = None
    question_source: Path | None = None
    _hash_cache: dict[str, str] | None = field(default=None, init=False, repr=False)

    @classmethod
    def load(cls, package: Path) -> "EvalPackage":
        root, manifest_path = _manifest_path(package)
        raw_manifest = _load_json_or_jsonl(manifest_path)
        if not isinstance(raw_manifest, dict):
            raise PackageError("PACKAGE_MANIFEST_MUST_BE_OBJECT")
        manifest = raw_manifest.get("package", raw_manifest)
        if not isinstance(manifest, dict):
            raise PackageError("PACKAGE_MANIFEST_PACKAGE_MUST_BE_OBJECT")
        schema = (
            manifest.get("schema")
            or manifest.get("package_schema")
            or manifest.get("schema_version")
            or raw_manifest.get("schema")
            or raw_manifest.get("package_schema")
            or raw_manifest.get("schema_version")
        )
        if schema != PACKAGE_SCHEMA:
            raise PackageError(f"PACKAGE_SCHEMA_UNSUPPORTED: expected {PACKAGE_SCHEMA}, got {schema!r}")

        scope = str(manifest.get("scope") or manifest.get("evaluation", {}).get("scope") or "global")
        if scope not in ("global", "document_local"):
            raise PackageError(f"PACKAGE_SCOPE_UNSUPPORTED: {scope}")
        condition = str(manifest.get("condition", manifest.get("evaluation", {}).get("condition", "native")))
        condition_fields = manifest.get("conditions") if isinstance(manifest.get("conditions"), Mapping) else {}
        representation_value = (
            manifest.get("ingest_representation")
            or manifest.get("evaluation", {}).get("ingest_representation")
            or condition_fields.get("ingest_representation")
            or ""
        )
        representation_aliases = {
            "candidate_text": "candidate_markdown",
            "candidate-text": "candidate_markdown",
            "candidate_text_controlled": "candidate_markdown",
            "controlled_parsed_text": "candidate_markdown",
            "source_pdf": "source_document",
            "native_pdf": "source_document",
            "source_pdf_native": "source_document",
        }
        ingest_representation = representation_aliases.get(str(representation_value).strip().casefold(), str(representation_value).strip().casefold())

        document_spec = manifest.get("documents", manifest.get("documents_path"))
        if document_spec is None:
            document_spec = _artifact_path(manifest, raw_manifest, root, ("corpus.jsonl", "corpus"))
        if document_spec is None:
            default_dir = root / "documents"
            if not default_dir.exists():
                raise PackageError("PACKAGE_DOCUMENTS_MISSING")
            document_values: Any = [str(path.relative_to(root)) for path in sorted(default_dir.rglob("*")) if path.is_file()]
            document_source = None
        elif isinstance(document_spec, list):
            document_values, document_source = iter(document_spec), None
        else:
            document_values, document_source = _iter_records(document_spec, root, "documents")

        documents: list[Document] = []
        documents_by_id: dict[str, Document] = {}
        scope_groups: dict[str, list[Document]] = {}
        observed_representations: set[str] = set()
        for ordinal, item in enumerate(document_values, start=1):
            if isinstance(item, str):
                item = {"path": item}
            if not isinstance(item, dict):
                raise PackageError(f"PACKAGE_DOCUMENT_RECORD_INVALID: {ordinal}")
            metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
            explicit_id = item.get("id") or item.get("document_id") or item.get("doc_id") or item.get("file_id")
            scope_id = sys.intern(str(item.get("scope_id") or explicit_id or f"document-{ordinal:04d}"))
            declared_role = str(item.get("ingest_role") or metadata.get("ingest_role") or "").strip().casefold()
            role_aliases = {
                "candidate": "candidate_text",
                "candidate_markdown": "candidate_text",
                "image_description": "candidate_text",
                "source_pdf": "source_document",
                "pdf": "source_document",
            }
            ingest_role = role_aliases.get(declared_role, declared_role)
            if not ingest_role:
                candidate_like = bool(
                    scope == "document_local"
                    and (
                        (explicit_id not in (None, "") and str(explicit_id) != scope_id)
                        or "controlled" in condition.casefold()
                        or (item.get("content") is not None and str(item.get("media_type", "")).casefold() != "application/pdf")
                    )
                )
                ingest_role = "candidate_text" if candidate_like else "source_document"
            path_values = [
                item.get("binary_path"),
                item.get("text_path"),
                item.get("path"),
                item.get("file"),
                item.get("file_path"),
                item.get("source"),
            ]
            document_path: Path | None = None
            content: str | None = str(item.get("content")) if item.get("content") is not None else None
            declared_path: Path | None = None
            # Candidate rows are logical retrieval units.  Their shared JSONL
            # or image source is never an upload artifact; inline content is
            # aggregated into one scope-local Markdown file below.
            for path_value in ([] if ingest_role == "candidate_text" else path_values):
                if path_value in (None, ""):
                    continue
                candidate = Path(str(path_value)).expanduser()
                candidate = (candidate if candidate.is_absolute() else root / candidate).resolve()
                declared_path = declared_path or candidate
                if candidate.exists() and candidate.is_file():
                    document_path = candidate
                    break
            document_id = sys.intern(str(
                explicit_id
                or (document_path.stem if document_path is not None else f"document-{ordinal:04d}")
            ))
            if document_id in documents_by_id:
                raise PackageError(f"PACKAGE_DOCUMENT_ID_DUPLICATE: {document_id}")
            locator = metadata.get("source_record_locator")
            locator_id = locator.get("id") if isinstance(locator, Mapping) else None
            document = Document(
                    document_id=document_id,
                    scope_id=scope_id,
                    ingest_role=sys.intern(ingest_role),
                    package_path=document_path,
                    content=content,
                    media=_media_tuple(
                        item.get("media", item.get("modality", item.get("media_type", item.get("mime_type")))),
                        document_path,
                    ),
                    declared_hash=str(item.get("sha256") or "").removeprefix("sha256:"),
                    marker_page=first_value(metadata, ("page_number", "page", "page_id")),
                    marker_layout=first_value(metadata, ("layout_id", "layout_type")),
                    marker_quote=first_value(metadata, ("quote_id", "quote")) or locator_id,
                    pages=int(metadata.get("pages", item.get("pages", 0)) or 0),
                )
            documents.append(document)
            documents_by_id[document_id] = document
            scope_groups.setdefault(document.scope_id, []).append(document)
            observed_representations.add(
                "candidate_markdown" if document.ingest_role == "candidate_text" else "source_document"
            )
        if not ingest_representation:
            if len(observed_representations) > 1:
                raise PackageError("PACKAGE_INGEST_REPRESENTATION_REQUIRED_FOR_MIXED_ROLES")
            ingest_representation = next(iter(observed_representations), "source_document")
        if ingest_representation not in {"candidate_markdown", "source_document"}:
            raise PackageError(f"PACKAGE_INGEST_REPRESENTATION_UNSUPPORTED: {ingest_representation}")

        question_spec = manifest.get("questions", manifest.get("questions_path"))
        if question_spec is None:
            question_spec = _artifact_path(manifest, raw_manifest, root, ("questions.jsonl", "questions"))
        if question_spec is None:
            for name in ("questions.jsonl", "questions.json"):
                if (root / name).exists():
                    question_spec = name
                    break
        if question_spec is None:
            raise PackageError("PACKAGE_QUESTIONS_MISSING")
        if isinstance(question_spec, list):
            question_values, question_source = iter(question_spec), None
        else:
            question_values, question_source = _iter_records(question_spec, root, "questions")

        gold_spec = manifest.get("gold", manifest.get("gold_path"))
        if gold_spec is None:
            gold_spec = _artifact_path(manifest, raw_manifest, root, ("gold.jsonl", "gold"))
        gold_by_question: dict[str, dict[str, Any]] = {}
        gold_count = 0
        if gold_spec is not None:
            gold_values, _ = (
                (iter(gold_spec), None)
                if isinstance(gold_spec, list)
                else _iter_records(gold_spec, root, "gold")
            )
            for ordinal, gold_item in enumerate(gold_values, start=1):
                gold_count += 1
                if not isinstance(gold_item, dict):
                    raise PackageError(f"PACKAGE_GOLD_RECORD_INVALID: {ordinal}")
                gold_id = str(gold_item.get("id") or gold_item.get("question_id") or "")
                if gold_id:
                    if gold_id in gold_by_question:
                        raise PackageError(f"PACKAGE_GOLD_ID_DUPLICATE: {gold_id}")
                    gold_by_question[gold_id] = _compact_question_raw(gold_item)

        questions: list[Question] = []
        seen_questions: set[str] = set()
        for ordinal, item in enumerate(question_values, start=1):
            if not isinstance(item, dict):
                raise PackageError(f"PACKAGE_QUESTION_RECORD_INVALID: {ordinal}")
            question_id = str(item.get("id") or item.get("question_id") or f"question-{ordinal:04d}")
            gold_item = gold_by_question.get(question_id, {})
            if gold_item:
                merged_item = dict(gold_item)
                merged_item.update(item)
                item = merged_item
            if question_id in seen_questions:
                raise PackageError(f"PACKAGE_QUESTION_ID_DUPLICATE: {question_id}")
            seen_questions.add(question_id)
            text = str(item.get("question", item.get("query", item.get("text", ""))))
            if not text.strip():
                raise PackageError(f"PACKAGE_QUESTION_TEXT_MISSING: {question_id}")
            scope_references = _ids(item.get("scope_doc_ids")) if scope == "document_local" else []
            explicit_document_ids = [] if scope_references else _ids(
                item.get("document_ids", item.get("documents", item.get("document_id", item.get("file_id"))))
            )
            if scope == "document_local" and not scope_references:
                scope_references = list(explicit_document_ids)
            image_values = item.get("images", item.get("image_paths", item.get("image_path", [])))
            image_paths = [
                (root / str(value)).resolve() for value in _ids(image_values)
            ]
            for image_path in image_paths:
                if not image_path.exists():
                    raise PackageError(f"PACKAGE_QUESTION_IMAGE_MISSING: {image_path}")
            media_value = item.get("media", item.get("modality"))
            question_media = _normal_media(media_value)
            if image_paths and "image" not in question_media:
                question_media.append("image")
            answer = str(
                item.get(
                    "answer",
                    item.get(
                        "reference_answer",
                        item.get("gold_answer", item.get("expected_answer", "")),
                    ),
                )
                or ""
            )
            unknown_ids = [
                document_id
                for document_id in explicit_document_ids
                if document_id not in documents_by_id and document_id not in scope_groups
            ]
            if unknown_ids:
                raise PackageError(f"PACKAGE_QUESTION_DOCUMENT_UNKNOWN: {question_id}: {unknown_ids}")
            explicit_document_ids = [document_id for document_id in explicit_document_ids if document_id in documents_by_id]
            resolved_scope_ids: list[str] = []
            unknown_scopes: list[str] = []
            for reference in scope_references:
                if reference in scope_groups:
                    resolved_scope_ids.append(reference)
                elif reference in documents_by_id:
                    resolved_scope_ids.append(documents_by_id[reference].scope_id)
                else:
                    unknown_scopes.append(reference)
            if unknown_scopes:
                raise PackageError(f"PACKAGE_QUESTION_SCOPE_UNKNOWN: {question_id}: {unknown_scopes}")
            resolved_scope_ids = list(dict.fromkeys(resolved_scope_ids))
            if scope == "document_local" and not resolved_scope_ids and len(scope_groups) == 1:
                resolved_scope_ids = [next(iter(scope_groups))]
            questions.append(
                Question(
                    question_id=question_id,
                    text=text,
                    document_ids=explicit_document_ids,
                    scope_ids=resolved_scope_ids,
                    answer=answer,
                    media=question_media,
                    image_paths=image_paths,
                    raw=_compact_question_raw(item),
                )
            )
        del gold_by_question

        if not documents:
            raise PackageError("PACKAGE_DOCUMENTS_EMPTY")
        if not questions:
            raise PackageError("PACKAGE_QUESTIONS_EMPTY")
        if condition == "text-only-no-mllm":
            _validate_declared_counts(
                manifest,
                {"documents": len(documents), "questions": len(questions), "gold": gold_count},
            )
            if manifest.get("mllm_required") is not False:
                raise PackageError("TEXT_ONLY_MLLM_REQUIRED_MUST_BE_FALSE")
            if str(manifest.get("image_llm", "")).strip().upper() != "NOT_APPLICABLE":
                raise PackageError("TEXT_ONLY_IMAGE_LLM_MUST_BE_NOT_APPLICABLE")
            non_text_media = {
                media
                for document in documents
                for media in document.media
                if media not in {"text", "markdown"}
            }
            if non_text_media:
                raise PackageError(
                    f"TEXT_ONLY_DOCUMENT_MEDIA_UNSUPPORTED:{sorted(non_text_media)}"
                )

        dataset_value = manifest.get("dataset", manifest.get("dataset_id", "UNKNOWN"))
        dataset = str(dataset_value.get("name") if isinstance(dataset_value, dict) else dataset_value)
        revision = str(
            manifest.get(
                "revision",
                manifest.get("dataset_revision", manifest.get("package_revision", manifest.get("protocol_tag", "UNKNOWN"))),
            )
        )
        split = str(manifest.get("split", condition_fields.get("split", "UNKNOWN")))
        protocol_tag = str(manifest.get("protocol_tag", manifest.get("protocol", "ADAPTED_PROTOCOL")))
        return cls(
            root=root,
            manifest_path=manifest_path,
            manifest=manifest,
            dataset=dataset,
            revision=revision,
            split=split,
            scope=scope,
            protocol_tag=protocol_tag,
            condition=condition,
            documents=documents,
            questions=questions,
            scope_groups=scope_groups,
            documents_by_id=documents_by_id,
            ingest_representation=ingest_representation,
            document_source=document_source,
            question_source=question_source,
        )

    @property
    def document_map(self) -> dict[str, Document]:
        return self.documents_by_id

    @property
    def image_question_count(self) -> int:
        return sum("image" in question.media for question in self.questions)

    @property
    def pages(self) -> int:
        return sum(document.pages for document in self.documents)

    @property
    def pages_known(self) -> bool:
        return all(document.pages > 0 for document in self.documents)

    def required_document_ids(self, question: Question) -> list[str]:
        if self.scope == "global":
            return [document.document_id for document in self.documents]
        if len(question.scope_ids) != 1:
            return []
        return [document.document_id for document in self.scope_groups.get(question.scope_ids[0], [])]

    def ledger_reference(self, question: Question) -> dict[str, Any]:
        resource_keys = [GLOBAL_RESOURCE] if self.scope == "global" else list(question.scope_ids)
        if self.scope == "global":
            count = len(self.documents)
        else:
            count = sum(len(self.scope_groups.get(scope_id, ())) for scope_id in question.scope_ids)
        result: dict[str, Any] = {"resource_keys": resource_keys, "document_count": count}
        if count <= 32:
            result["document_ids"] = self.required_document_ids(question)
        else:
            result["document_ids_omitted"] = True
        return result

    def target_documents(self) -> list[tuple[str, list[Document]]]:
        if self.scope == "global":
            return [(GLOBAL_RESOURCE, list(self.documents))]
        return list(self.scope_groups.items())

    def hashes(self) -> dict[str, str]:
        if self._hash_cache is not None:
            return dict(self._hash_cache)
        question_hash = (
            _sha256_file(self.question_source)
            if self.question_source is not None and self.question_source.exists()
            else _sha256_canonical_records(question.raw for question in self.questions)
        )
        self._hash_cache = {
            "manifest": f"sha256:{_sha256_file(self.manifest_path)}",
            "questions": f"sha256:{question_hash}",
            "documents": f"sha256:{_sha256_canonical_records({'id': document.document_id, 'scope_id': document.scope_id, 'media': document.media, 'sha256': document.source_hash} for document in self.documents)}",
            "gold": f"sha256:{_sha256_canonical_records({'id': question.question_id, 'answer': question.answer, 'gold': question.gold} for question in self.questions)}",
        }
        return dict(self._hash_cache)

    def record(self) -> dict[str, Any]:
        return {
            "schema": PACKAGE_SCHEMA,
            "benchmark_scope": {
                "evaluation_order": CONTRACTS["benchmark_scope"]["evaluation_order"],
                "excluded_datasets": CONTRACTS["benchmark_scope"]["excluded_datasets"],
                "denominator_source": CONTRACTS["benchmark_scope"]["denominator_source"],
                "paper_full_corpus_required": False,
                "availability_policy": CONTRACTS["benchmark_scope"]["availability_policy"],
            },
            "root": str(self.root),
            "manifest": str(self.manifest_path),
            "dataset": self.dataset,
            "revision": self.revision,
            "split": self.split,
            "scope": self.scope,
            "scope_count": 1 if self.scope == "global" else len(self.scope_groups),
            "scope_sizes": {key: len(documents) for key, documents in self.scope_groups.items()},
            "protocol_tag": self.protocol_tag,
            "condition": self.condition,
            "ingest_representation": self.ingest_representation,
            "document_count": len(self.documents),
            "document_preview": [
                {
                    "id": document.document_id,
                    "scope_id": document.scope_id,
                    "ingest_role": document.ingest_role,
                    "path": str(document.package_path) if document.package_path else None,
                    "media": document.media,
                    "sha256": document.source_hash,
                }
                for document in self.documents[:20]
            ],
            "documents_omitted": max(0, len(self.documents) - 20),
            "question_count": len(self.questions),
            "question_preview": [
                {
                    "id": question.question_id,
                    "document_ids": question.document_ids,
                    "scope_ids": question.scope_ids,
                    "media": question.media,
                    "has_gold_answer": bool(question.answer),
                }
                for question in self.questions[:20]
            ],
            "questions_omitted": max(0, len(self.questions) - 20),
            "hashes": self.hashes(),
        }


@dataclass
class ResourceIngestPlan:
    representation: str
    artifacts: list[Document]
    unsupported_candidates: dict[str, str]


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    base_url: str
    api_key: str
    model: str
    image_model: str = ""
    embedding_model: str = ""
    embedding_dimension: int | None = None
    api_key_env: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base_url": self.base_url,
            "api_key_loaded": bool(self.api_key and not self.api_key.startswith("<")),
            "model": self.model,
            "image_model": self.image_model,
            "embedding_model": self.embedding_model,
            "embedding_dimension": self.embedding_dimension,
            "api_key_env": self.api_key_env,
        }


def _merge_env_files(system: str) -> dict[str, str]:
    # The root .env is the sole credential entry point.  Platform contract
    # files still document runtime layout, but are not credential sources.
    values: dict[str, str] = parse_dotenv(ROOT / ".env")
    # Process environment is the explicit operator override.
    values.update({key: value for key, value in os.environ.items()})
    return values


def _reject_taas(values: Iterable[Any]) -> None:
    for value in values:
        text = str(value or "").lower()
        if any(marker in text for marker in SECRET_WORDS):
            raise RunnerError("FROZEN_PROVIDER_REQUIRED: TaaS/Qianfan/MatrixOrigin provider is forbidden")


def _validate_maas_base_url(value: Any) -> None:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RunnerError("MAAS_ONLY_BASE_URL_REQUIRED") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != MAAS_HOST
        or port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise RunnerError("MAAS_ONLY_BASE_URL_REQUIRED")


def _validate_deepseek_base_url(value: Any) -> None:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise RunnerError("DEEPSEEK_OFFICIAL_BASE_URL_REQUIRED") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != DEEPSEEK_HOST
        or port not in (None, 443)
        or parsed.username
        or parsed.password
    ):
        raise RunnerError("DEEPSEEK_OFFICIAL_BASE_URL_REQUIRED")


def _reject_dify_adapters(values: Iterable[Any], *, allow_generic_compat: bool) -> None:
    checked = [
        value
        for value in values
        if not (allow_generic_compat and value == DIFY_GENERIC_OAI_ADAPTER)
    ]
    _reject_taas(checked)


def _provider_profiles(
    args: argparse.Namespace,
    env: dict[str, str],
    *,
    image_question_count: int = 0,
) -> dict[str, ProviderProfile]:
    """Resolve the frozen DeepSeek-text + MaaS-embedding contract.

    Legacy MaaS/Qianfan text arguments remain parser-compatible only for
    checkpoint diagnostics; they are never materialized into a text profile.
    This prevents a stale platform .env from becoming an implicit fallback or
    silently spending against the retired GLM text route.
    """

    maas_base = str(
        getattr(args, "maas_base_url", None)
        or value_from(env, "MAAS_BASE_URL", default=str(CONTRACTS["defaults"]["maas_base_url"]))
    ).rstrip("/")
    _validate_maas_base_url(maas_base)
    # The derived package is text-only. Keep the legacy CLI field readable for
    # old checkpoints, but never carry a vision model into this contract.
    maas_vl_model = "NOT_APPLICABLE"
    text_llm_provider = str(
        getattr(args, "text_llm_provider", None)
        or value_from(
            env,
            "COMPETITOR_TEXT_LLM_PROVIDER",
            default=str(CONTRACTS["defaults"].get("text_llm_provider", "deepseek-official")),
        )
    ).casefold()
    if text_llm_provider not in {"deepseek", "deepseek-official", "dsv4f"}:
        raise RunnerError("DEEPSEEK_TEXT_PROVIDER_REQUIRED")
    deepseek_base = str(
        getattr(args, "deepseek_base_url", None)
        or value_from(
            env,
            "DEEPSEEK_BASE_URL",
            default=str(CONTRACTS["defaults"].get("deepseek_base_url", DEFAULT_DEEPSEEK_BASE_URL)),
        )
    ).rstrip("/")
    _validate_deepseek_base_url(deepseek_base)
    deepseek_llm_model = str(
        getattr(args, "deepseek_llm_model", None)
        or value_from(
            env,
            "DEEPSEEK_LLM_MODEL",
            default=str(CONTRACTS["defaults"].get("deepseek_llm_model", DEFAULT_DEEPSEEK_LLM_MODEL)),
        )
    )
    embedding_provider = value_from(env, "COMPETITOR_EMBEDDING_PROVIDER", default="maas").casefold()
    if embedding_provider != "maas":
        raise RunnerError("MAAS_ONLY_EMBEDDING_PROVIDER_REQUIRED")
    embedding_model = str(
        getattr(args, "maas_embedding_model", None)
        or value_from(
            env,
            "MAAS_EMBEDDING_MODEL",
            default=str(CONTRACTS["defaults"]["maas_embedding_model"]),
        )
    )
    dimension = int(
        getattr(args, "maas_embedding_dimension", None)
        or value_from(
            env,
            "MAAS_EMBEDDING_DIMENSION",
            default=str(CONTRACTS["defaults"]["maas_embedding_dimension"]),
        )
    )
    expected_llm = str(CONTRACTS["defaults"].get("deepseek_llm_model", DEFAULT_DEEPSEEK_LLM_MODEL))
    expected_embedding = str(CONTRACTS["defaults"]["maas_embedding_model"])
    expected_dimension = int(CONTRACTS["defaults"]["maas_embedding_dimension"])
    if deepseek_llm_model != expected_llm:
        raise RunnerError(f"DEEPSEEK_TEXT_MODEL_REQUIRED:{expected_llm}")
    if embedding_model != expected_embedding or dimension != expected_dimension:
        raise RunnerError(f"MAAS_ONLY_EMBEDDING_REQUIRED:{expected_embedding}/{expected_dimension}")
    _reject_taas([
        maas_base,
        deepseek_base,
        deepseek_llm_model,
        maas_vl_model,
        embedding_model,
    ])
    embedding = ProviderProfile(
        name="maas",
        base_url=maas_base,
        api_key=value_from(env, "MAAS_API_KEY", "MAAS_APIKEY"),
        model="",
        embedding_model=embedding_model,
        embedding_dimension=dimension,
        api_key_env="MAAS_API_KEY",
    )
    text_llm = ProviderProfile(
        name="deepseek-official",
        base_url=deepseek_base,
        api_key=value_from(env, DEEPSEEK_API_KEY_ENV),
        model=deepseek_llm_model,
        api_key_env=DEEPSEEK_API_KEY_ENV,
    )
    vision = ProviderProfile(
        name="maas",
        base_url=maas_base,
        api_key=value_from(env, "MAAS_API_KEY", "MAAS_APIKEY"),
        model="NOT_APPLICABLE",
        api_key_env="MAAS_API_KEY",
    )
    return {"maas": embedding, "llm": text_llm, "vision": vision}


def _metric_contract(args: argparse.Namespace, package: EvalPackage) -> dict[str, Any]:
    if getattr(args, "command", "all") == "qa":
        return {
            "evaluation_scope": "native_qa_quality_only",
            "direct_retrieval": "NOT_RUN",
            "primary": [
                "answer_contains_gold_rate",
                "normalized_em",
                "token_f1",
                "tdas",
            ],
            "secondary": [
                "searchable_ready_rate",
                "accepted_document_rate",
                "first_pass_availability",
                "answer_non_empty_rate",
                "citation_locator_validity",
                "citation_entailment_precision",
                "unsupported_count",
            ],
            "latency": [],
            "deferred_performance": [
                "retrieval_latency",
                "generation_latency",
                "e2e_latency",
                "throughput",
                "resource_usage",
                "cost",
            ],
            "slices": ["domain", "question_type", "evidence_modality", "single_or_multi_page", "media"],
            "valid_denominator_policy": "Every quality metric records planned_n, valid_n, failed_n, unsupported_n and reason. FAILED, BLOCKED, TIMEOUT, EMPTY and UNSUPPORTED remain in the planned denominator; unsupported metrics are N/A rather than silently removed.",
            "scope": package.scope,
            "judge": "N/A: no judge request is made by this runner; TDAS remains unsupported unless a frozen judge contract is supplied by a future package.",
        }
    return {
        "evaluation_scope": "retrieval_and_native_qa",
        "direct_retrieval": "RUN",
        "primary": [
            "searchable_ready_rate",
            "evidence_recall_at_1",
            "evidence_recall_at_3",
            "evidence_recall_at_5",
            "evidence_recall_at_10",
            "mrr",
            "answer_contains_gold_rate",
            "normalized_em",
            "token_f1",
            "tdas",
        ],
        "secondary": [
            "accepted_document_rate",
            "first_pass_availability",
            "answer_non_empty_rate",
            "retrieval_trace_completeness",
            "citation_locator_validity",
            "citation_entailment_precision",
            "unsupported_count",
        ],
        "latency": [
            "ingest_upload_p50",
            "ingest_upload_p95",
            "retrieval_p50",
            "retrieval_p95",
            "generation_p50",
            "generation_p95",
            "e2e_p50",
            "e2e_p95",
        ],
        "slices": ["domain", "question_type", "evidence_modality", "single_or_multi_page", "media"],
        "valid_denominator_policy": "Every metric records planned_n, valid_n, failed_n, unsupported_n and reason. FAILED, BLOCKED, TIMEOUT, EMPTY and UNSUPPORTED remain in the planned denominator; unsupported metrics are N/A rather than silently removed.",
        "scope": package.scope,
        "top_k": list(DEFAULT_TOP_K),
        "judge": "N/A: no judge request is made by this runner; TDAS remains unsupported unless a frozen judge contract is supplied by a future package.",
        "per_stage": {
            "retrieval": "Question-level initial attempts; response/empty are valid retrieval observations, request failures are failed observations.",
            "qa": "Question-level initial attempts; non-empty and empty HTTP responses are valid observations, request failures are failed observations.",
        },
    }


def _denominator_contract(package: EvalPackage, repeats: int) -> dict[str, Any]:
    planned = len(package.questions) * repeats
    return {
        "unit": "one question x one initial repeat",
        "planned_initial_attempts": planned,
        "planned_questions": len(package.questions),
        "repeats": repeats,
        "all_statuses_count_in_planned_n": sorted(TERMINAL_STATUSES),
        "valid_statuses": ["SUCCESS", "EMPTY"],
        "failed_statuses": ["FAILED", "BLOCKED", "TIMEOUT", "INTERRUPTED"],
        "unsupported_statuses": ["UNSUPPORTED"],
        "retry_policy": "No implicit retry replaces an initial row. Bounded readiness retries and explicit diagnostic retries are recorded in the raw HTTP/terminal details; a later retry must use a new run_id or a same-attempt resume.",
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * fraction))))
    return round(ordered[index], 3)


def _tokens(value: str) -> list[str]:
    return re.findall(r"[\w\u4e00-\u9fff]+", value.lower(), flags=re.UNICODE)


_MAXKB_CONTEXT_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "does",
    "for",
    "from",
    "how",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "was",
    "what",
    "which",
    "who",
    "with",
}


def _maxkb_external_context(
    hits: Iterable[Any],
    question: str,
    *,
    max_chars: int = MAXKB_EXTERNAL_CONTEXT_CHARS,
    max_chunks: int = MAXKB_EXTERNAL_MAX_CHUNKS,
) -> str:
    """Select a bounded lexical evidence window from MaxKB hit-test rows.

    MaxKB 2.10.4 exposes ``chunks`` as the product's internal paragraph
    segmentation, but its admin hit-test row also contains the entire source
    paragraph in ``content``. Prefer chunks that share non-stopword terms with
    the question; this approximates the product's native context selection and
    prevents the external MaaS compatibility path from sending entire books or
    news articles. If no chunk matches, retain only a short prefix per hit so
    an unanswerable question remains auditable without shipping the full corpus.
    """

    budget = max(1, int(max_chars))
    chunk_limit = max(1, int(max_chunks))
    terms = set(_tokens(question)) - _MAXKB_CONTEXT_STOPWORDS
    ranked: list[tuple[int, float, int, int, str]] = []
    fallback: list[tuple[float, int, str]] = []
    for hit_index, hit in enumerate(hits):
        if not isinstance(hit, Mapping):
            continue
        try:
            score_value = float(hit.get("comprehensive_score", hit.get("similarity", 0)) or 0)
        except (TypeError, ValueError):
            score_value = 0.0
        content = str(
            hit.get("content")
            or hit.get("paragraph_content")
            or hit.get("text")
            or hit.get("document_content")
            or ""
        ).strip()
        if content:
            fallback.append((score_value, hit_index, content))
        chunks = hit.get("chunks")
        if not isinstance(chunks, list):
            continue
        for chunk_index, chunk in enumerate(chunks):
            text = str(chunk or "").strip()
            if not text:
                continue
            chunk_terms = set(_tokens(text))
            overlap = len(terms & chunk_terms)
            if overlap:
                ranked.append((overlap, score_value, hit_index, chunk_index, text))

    ranked.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
    selected: list[str] = []
    used_chars = 0
    for _overlap, _score, hit_index, chunk_index, text in ranked[:chunk_limit]:
        row = f"[hit {hit_index + 1} chunk {chunk_index + 1}] {text}"
        if used_chars + len(row) > budget:
            remaining = budget - used_chars
            if remaining > 80:
                selected.append(row[:remaining])
            break
        selected.append(row)
        used_chars += len(row) + 2
    if selected:
        return "\n\n".join(selected)

    fallback.sort(key=lambda item: (-item[0], item[1]))
    for _score, hit_index, content in fallback:
        row = f"[hit {hit_index + 1} prefix] {content[:512]}"
        if used_chars + len(row) > budget:
            remaining = budget - used_chars
            if remaining > 80:
                selected.append(row[:remaining])
            break
        selected.append(row)
        used_chars += len(row) + 2
    return "\n\n".join(selected) or "(No relevant MaxKB context was retrieved.)"


def _token_f1(prediction: str, answer: str) -> float | None:
    gold = _tokens(answer)
    predicted = _tokens(prediction)
    if not gold:
        return None
    if not predicted:
        return 0.0
    gold_counts = Counter(gold)
    predicted_counts = Counter(predicted)
    overlap = sum((gold_counts & predicted_counts).values())
    if not overlap:
        return 0.0
    precision = overlap / len(predicted)
    recall = overlap / len(gold)
    return 2 * precision * recall / (precision + recall)


def _answer_metrics(answer: str, gold: str) -> dict[str, Any]:
    normalized_answer = normalize(answer)
    normalized_gold = normalize(gold)
    return {
        "answer_non_empty": bool(answer.strip()),
        "contains_gold": bool(normalized_gold and normalized_gold in normalized_answer),
        "normalized_em": bool(normalized_gold and normalized_gold == normalized_answer),
        "token_f1": _token_f1(answer, gold),
    }


def _hit_markers(hit: Any) -> str:
    try:
        return json.dumps(hit, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return str(hit).lower()


def _candidate_ids_from_hit(hit: Any) -> set[str]:
    values: set[str] = set()
    if isinstance(hit, Mapping):
        for key in ("doc_id", "document_id", "candidate_id", "source_id"):
            if hit.get(key) not in (None, ""):
                values.add(str(hit[key]))
        metadata = hit.get("metadata")
        if isinstance(metadata, Mapping):
            values.update(_candidate_ids_from_hit(metadata))
    try:
        marker_text = json.dumps(hit, ensure_ascii=False)
    except (TypeError, ValueError):
        marker_text = str(hit)
    for match in re.finditer(r"competitor-eval-candidate:\s*(\{.*?\})\s*-->", marker_text, flags=re.DOTALL):
        try:
            marker = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(marker, Mapping) and marker.get("doc_id") not in (None, ""):
            values.add(str(marker["doc_id"]))
    return values


def _gold_document_ids(question: Question) -> set[str]:
    gold = question.gold
    values: list[Any] = []
    if isinstance(gold, Mapping):
        for key in ("document_ids", "documents", "document_id", "file_ids", "file_id", "source_ids", "sources"):
            if key in gold:
                values.extend(_ids(gold[key]))
    for key in (
        "gold_document_ids",
        "gold_doc_ids",
        "relevant_document_ids",
        "relevant_documents",
        "gold_documents",
    ):
        if key in question.raw:
            values.extend(_ids(question.raw[key]))
    return set(values)


def _retrieval_metrics(question: Question, hits: list[Any], top_k: int) -> dict[str, Any]:
    gold_ids = _gold_document_ids(question)
    if not gold_ids:
        return {
            "metric_status": "UNSUPPORTED",
            "reason": "NO_GOLD_DOCUMENT_IDS",
            "evidence_recall": None,
            "mrr": None,
        }
    ranked = [(_candidate_ids_from_hit(hit), _hit_markers(hit)) for hit in hits[:top_k]]
    hit_ranks = [
        index
        for index, (candidate_ids, marker_text) in enumerate(ranked, start=1)
        if candidate_ids.intersection(gold_ids) or any(document_id.lower() in marker_text for document_id in gold_ids)
    ]
    hit = bool(hit_ranks)
    return {
        "metric_status": "OK",
        "gold_document_count": len(gold_ids),
        "evidence_recall": 1.0 if hit else 0.0,
        "mrr": 1.0 / hit_ranks[0] if hit_ranks else 0.0,
        "hit_at_k": hit,
    }


def _exception_status(exc: BaseException) -> str:
    text = str(exc).lower()
    if isinstance(exc, ContractUnsupported) or "unsupported" in text:
        return "UNSUPPORTED"
    if "timeout" in text or "timed out" in text:
        return "TIMEOUT"
    if "interrupt" in text:
        return "INTERRUPTED"
    if isinstance(exc, (ProviderUnavailable,)) or "missing" in text or "not configured" in text:
        return "BLOCKED"
    return "FAILED"


def _qa_session_id(question_id: str, repeat_id: int) -> str:
    return f"{question_id}#repeat-{repeat_id}"


def _qa_session_question(question: Question, repeat_id: int) -> Question:
    return replace(question, question_id=_qa_session_id(question.question_id, repeat_id))


def _error_text(exc: BaseException) -> str:
    return redact_text(str(exc))[:2000]


@dataclass
class RunnerContext:
    args: argparse.Namespace
    package: EvalPackage
    root: Path = field(init=False)
    env: dict[str, str] = field(init=False)
    profiles: dict[str, ProviderProfile] = field(init=False)
    progress: Any = field(init=False)
    start_record: dict[str, Any] = field(init=False)
    _preflight_cache: dict[str, Any] | None = field(default=None, init=False)
    _ingest_plans: dict[str, ResourceIngestPlan] = field(default_factory=dict, init=False)
    _terminal_keys_cache: set[tuple[str, str, int]] | None = field(default=None, init=False)
    _terminal_rows_by_key: dict[tuple[str, str, int], dict[str, Any]] = field(default_factory=dict, init=False)
    _artifact_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)
    _state_lock: Any = field(default_factory=threading.RLock, init=False, repr=False)
    _http_clients: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    _artifact_http_counter: int = field(default=0, init=False, repr=False)
    _maas_qa_rate_limiter: Any = field(default=None, init=False, repr=False)
    _moi_artifacts: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    _moi_result_offsets: dict[Path, dict[str, int]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.env = _merge_env_files(self.args.system)
        if self.args.system == "moi_local" and _enabled(value_from(self.env, "MOI_EVAL_FIXTURE")) and not self.args.dry_run:
            raise RunnerError("TEST_FIXTURE_FORBIDDEN")
        if self.package.image_question_count != 0:
            raise RunnerError("TEXT_ONLY_IMAGE_QUESTION_COUNT_MUST_BE_0")
        self.args.qa_concurrency = _qa_concurrency(self.args, self.env)
        configured_retrieval = getattr(self.args, "retrieval_concurrency", None) or value_from(
            self.env, "COMPETITOR_EVAL_RETRIEVAL_CONCURRENCY", default="8"
        )
        self.args.retrieval_concurrency = max(1, int(configured_retrieval))
        if self.args.system == "fastgpt_local":
            self.args.fastgpt_ingest_concurrency = _fastgpt_ingest_concurrency(self.args, self.env)
        elif self.args.system == "maxkb_local":
            self.args.maxkb_ingest_concurrency = _maxkb_ingest_concurrency(self.args, self.env)
        self.profiles = _provider_profiles(
            self.args,
            self.env,
            image_question_count=self.package.image_question_count,
        )
        if self.args.system == "maxkb_local":
            configured_maxkb_embedding = value_from(self.env, "MAXKB_EMBEDDING_PROVIDER").casefold()
            if configured_maxkb_embedding and configured_maxkb_embedding != "maas":
                raise RunnerError("MAAS_ONLY_EMBEDDING_PROVIDER_REQUIRED")
        configured_fastgpt_embedding = value_from(self.env, "FASTGPT_EMBEDDING_PROVIDER").casefold()
        if configured_fastgpt_embedding and configured_fastgpt_embedding != "maas":
            raise RunnerError("MAAS_ONLY_EMBEDDING_PROVIDER_REQUIRED")
        interval = 0.0
        if self.args.system == "fastgpt_local":
            raw_interval = value_from(
                self.env,
                "FASTGPT_MAAS_QA_MIN_INTERVAL_SECONDS",
                default=str(FASTGPT_MAAS_QA_MIN_INTERVAL_DEFAULT),
            )
            try:
                interval = max(0.0, float(raw_interval))
            except (TypeError, ValueError):
                interval = FASTGPT_MAAS_QA_MIN_INTERVAL_DEFAULT
        elif self.args.system == "maxkb_local":
            raw_interval = value_from(
                self.env,
                "MAXKB_QA_MIN_INTERVAL_SECONDS",
                default=value_from(
                    self.env,
                    "MAXKB_LEGACY_QA_MIN_INTERVAL_SECONDS",
                    default=str(MAXKB_LEGACY_QA_MIN_INTERVAL_DEFAULT),
                ),
            )
            try:
                interval = max(0.0, float(raw_interval))
            except (TypeError, ValueError):
                interval = MAXKB_QA_MIN_INTERVAL_DEFAULT
        self._maas_qa_rate_limiter = _StartRateLimiter(interval)
        run_id = str(self.args.run_id or _now_run_id(self.args.system, self.package.dataset))
        if any(part in run_id for part in ("/", "\\", "..")):
            raise RunnerError("RUN_ID_INVALID")
        self.args.run_id = run_id
        self.root = Path(self.args.output_root).expanduser().resolve() / run_id
        self.root.mkdir(parents=True, exist_ok=True)
        self._artifact_http_counter = self._existing_http_counter()
        self.progress = self._progress()
        if self.args.system == "moi_local":
            self._materialize_moi_artifacts()
        self._prepare_immutable_artifacts()
        self._record_provider_adjustment()

    def _progress(self) -> Any:
        # Progress is imported from the existing helper only after the output
        # root exists.  Importing it never performs I/O outside this run.
        from mmdocir_competitor_eval import Progress  # type: ignore

        return Progress(self.root / "progress.jsonl")

    def _record_provider_adjustment(self) -> None:
        """Record an intentional provider change when resuming an old run.

        The original start record remains immutable.  A same-run operator
        adjustment is therefore recorded as a separately hashed artifact so
        the effective provider is still auditable without silently rewriting
        the checkpoint configuration.
        """
        expected = self._start_record()
        existing_provider = self.start_record.get("provider", {})
        expected_provider = expected.get("provider", {})
        if existing_provider.get("llm") == expected_provider.get("llm"):
            return
        adjustment = {
            "schema": "competitor-eval-resume-provider-adjustment-v1",
            "run_id": self.args.run_id,
            "source_start_record_sha256": _sha256_file(self.root / "start-record.json"),
            "previous": {
                "llm": existing_provider.get("llm"),
                "text_llm_provider": existing_provider.get("text_llm_provider"),
                "text_llm_model": existing_provider.get("text_llm_model"),
            },
            "effective": {
                "llm": expected_provider.get("llm"),
                "text_llm_provider": expected_provider.get("text_llm_provider"),
                "text_llm_model": expected_provider.get("text_llm_model"),
            },
            "reason": "operator_requested_text_only_provider_resume",
        }
        path = self.root / "resume-provider-adjustment.json"
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
            if current != adjustment:
                # A run may be resumed more than once with an intentional
                # provider/model change (for example dsv4p -> dsv4f). Keep the
                # previous effective adjustment auditable instead of treating
                # the next operator correction as a corrupt checkpoint.
                history = self.root / "resume-provider-adjustment-history.jsonl"
                historical = _read_jsonl(history)
                if current not in historical:
                    _write_jsonl(history, [current], append=True)
                json_dump(path, adjustment)
                _write_hash(path)
                return
            if not path.with_suffix(path.suffix + ".sha256").exists():
                _write_hash(path)
            return
        json_dump(path, adjustment)
        _write_hash(path)

    def _existing_http_counter(self) -> int:
        values = []
        for path in (self.root / "http").glob("*.json"):
            match = re.match(r"^(\d+)-", path.name)
            if match:
                values.append(int(match.group(1)))
        return max(values, default=0)

    def _protect_artifact_client(self, client: Any) -> Any:
        """Protect ArtifactHTTP's mutable counter and its artifact write hook.

        The production client exposes ``_save_http``.  The test doubles used by
        this runner expose ``_record`` instead, so both seams are guarded while
        the network request itself remains outside the lock.
        """

        if getattr(client, "_competitor_eval_artifact_guarded", False):
            return client
        save_http = getattr(client, "_save_http", None)
        if callable(save_http):
            def save_http_locked(operation: str, request_meta: dict[str, Any], response: Any, error: Any = None) -> None:
                with self._artifact_lock:
                    self._artifact_http_counter += 1
                    if hasattr(client, "counter"):
                        client.counter = self._artifact_http_counter - 1
                    save_http(operation, request_meta, response, error)

            setattr(client, "_save_http", save_http_locked)
        else:
            record = getattr(client, "_record", None)
            if callable(record):
                def record_locked(*args: Any, **kwargs: Any) -> Any:
                    with self._artifact_lock:
                        return record(*args, **kwargs)

                setattr(client, "_record", record_locked)
        setattr(client, "_competitor_eval_artifact_guarded", True)
        return client

    @property
    def contract(self) -> dict[str, Any]:
        return CONTRACTS["systems"][self.args.system]

    @property
    def resource_map_path(self) -> Path:
        return self.root / "resource-map.json"

    @property
    def terminal_path(self) -> Path:
        return self.root / "terminal-ledger.jsonl"

    def write_secret(self, resource_key: str, value: str) -> str:
        if not value:
            raise RunnerError("APP_API_KEY_EMPTY")
        with self._artifact_lock:
            secret_dir = self.root / "secrets"
            secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            secret_dir.chmod(0o700)
            ignore_path = secret_dir / ".gitignore"
            if not ignore_path.exists():
                ignore_path.write_text("*\n!.gitignore\n", encoding="utf-8")
            secret_path = secret_dir / f"{self.args.system}-{_safe_name(resource_key)}-app.key"
            secret_path.write_text(value.strip() + "\n", encoding="utf-8")
            secret_path.chmod(0o600)
            http_dir = self.root / "http"
            if http_dir.is_dir():
                for artifact in http_dir.glob("*.json"):
                    text = artifact.read_text(encoding="utf-8", errors="replace")
                    if value in text:
                        artifact.write_text(text.replace(value, "<redacted>"), encoding="utf-8")
                        _write_hash(artifact)
            return secret_path.relative_to(self.root).as_posix()

    def read_secret(self, relative_path: Any) -> str:
        if not relative_path:
            raise ProviderUnavailable("RESOURCE_APP_KEY_SECRET_PATH_MISSING")
        candidate = (self.root / str(relative_path)).resolve()
        if self.root != candidate and self.root not in candidate.parents:
            raise ProviderUnavailable("RESOURCE_APP_KEY_SECRET_PATH_OUTSIDE_RUN")
        if not candidate.is_file():
            raise ProviderUnavailable("RESOURCE_APP_KEY_SECRET_FILE_MISSING")
        value = candidate.read_text(encoding="utf-8", errors="replace").strip()
        if not value:
            raise ProviderUnavailable("RESOURCE_APP_KEY_SECRET_EMPTY")
        return value

    def _start_record(self) -> dict[str, Any]:
        text_llm = self.profiles["llm"]
        image_profile = None
        image_llm = "NOT_APPLICABLE"
        native_provider_key = ""
        native_provider = ""
        if self.args.system == "dify_local":
            native_provider_key = "DIFY_DEEPSEEK_LLM_PROVIDER"
            native_provider = value_from(self.env, native_provider_key)
        provider = {
            "model_egress": "external",
            "policy": "deepseek_text_maas_embedding",
            "llm": f"{text_llm.name}/{text_llm.model}",
            "text_llm_provider": text_llm.name,
            "text_llm_model": text_llm.model,
            "image_llm": image_llm,
            "image_model_status": "selected" if image_profile else "NOT_APPLICABLE",
            "image_question_count": self.package.image_question_count,
            "embedding": f"{self.profiles['maas'].name}/{self.profiles['maas'].embedding_model}/{self.profiles['maas'].embedding_dimension}",
            "embedding_provider": self.profiles["maas"].name,
            "text_llm": text_llm.public(),
            "maas": self.profiles["maas"].public(),
            "vision": self.profiles["vision"].public(),
            "thinking": dict(THINKING_CONFIG),
            "native_llm_provider": (
                native_provider or "NOT_CONFIGURED"
                if native_provider_key
                else "platform_selected_profile"
            ),
            "native_llm_provider_env": native_provider_key or "N/A",
            "native_llm_provider_status": (
                "configured" if native_provider else "required"
                if native_provider_key else "not_applicable"
            ),
            "keys_loaded": {
                "deepseek": bool(text_llm.api_key),
                "maas": bool(self.profiles["maas"].api_key),
            },
            "taas_used": False,
        }
        pipeline = {
            "parser": str(self.package.manifest.get("parser", "platform_native")),
            "ingest_representation": self.package.ingest_representation,
            "chunking": str(self.package.manifest.get("chunking", "platform_native")),
            "embedding": provider["embedding"],
            "retriever": str(self.package.manifest.get("retriever", "platform_native_semantic")),
            "reranker": str(self.package.manifest.get("reranker", "disabled")),
            "generator": provider["llm"],
            "image_generator": provider["image_llm"],
            "thinking": dict(THINKING_CONFIG),
            "judge": "N/A",
            "prompt_text": SHARED_PROMPT_TEXT,
            "prompt_version": SHARED_PROMPT_VERSION,
            "prompt_hash": SHARED_PROMPT_HASH,
            "top_k": list(DEFAULT_TOP_K),
            "context_budget": str(self.package.manifest.get("context_budget", "platform_default")),
            "max_output_tokens": int(self.package.manifest.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS) or DEFAULT_MAX_OUTPUT_TOKENS),
            "prompt_contract": {
                "version": SHARED_PROMPT_VERSION,
                "hash": SHARED_PROMPT_HASH,
                "text": SHARED_PROMPT_TEXT,
                "status": {
                    "moi_local": "propagated:cli_native_system_prompt",
                    "dify_local": "propagated:dify_native_pre_prompt",
                    "fastgpt_local": "propagated:isolated_app_system_prompt",
                    "maxkb_local": "propagated:external_deepseek_generation",
                }.get(self.args.system, "unsupported"),
            },
        }
        if self.args.system == "moi_local":
            moi_resources = {
                key: {
                    "config": str(value.get("config", "moi/config.json")),
                    "database": value.get("database"),
                    "vector_table": value.get("vector_table"),
                }
                for key, value in self._moi_artifacts.items()
            }
            pipeline["moi"] = {
                "adapter": "deterministic_fixture" if self.args.dry_run else "cli_native",
                "resources": moi_resources,
                "embedding": provider["embedding"],
                "generation": provider["llm"],
                "thinking": dict(THINKING_CONFIG),
                "include_page_images": False,
                "isolation": "unique MatrixOne database and vector table per run/resource",
            }
        elif self.args.system == "maxkb_local":
            pipeline["maxkb"] = {
                "retrieval_contract": "diagnostic_admin_contract",
                "qa_contract": "external_generation_from_maxkb_retrieval",
                "comparability": MAXKB_QA_COMPARABILITY,
                "native_thinking_disable": "UNPROVEN_MAXKB_2_10_4",
                "thinking": dict(THINKING_CONFIG),
            }
        timeout = {
            "service_readiness": float(self.args.service_timeout),
            "provider_probe": float(self.args.provider_timeout),
            "ingest_upload": float(self.args.upload_timeout),
            "index_wait": float(self.args.index_timeout),
            "retrieval": float(self.args.query_timeout),
            "qa": float(self.args.qa_timeout),
        }
        ingest_control = {}
        if self.args.system == "dify_local":
            ingest_control = {
                "document_local_max_indexing_scopes": _dify_max_indexing_scopes(self.args, self.env),
                "client_concurrency": "single_threaded_artifact_http",
                "state_checkpoint": "resource-map.json after every scope transition and readiness poll",
            }
        elif self.args.system == "fastgpt_local":
            ingest_control = {
                "fastgpt_ingest_concurrency": int(self.args.fastgpt_ingest_concurrency),
                "resource_concurrency": "independent_resources_only",
                "client_concurrency": "thread_safe_artifact_http",
                "state_checkpoint": "resource-map.json after each resource transition and collection readiness",
            }
        elif self.args.system == "maxkb_local":
            ingest_control = {
                "maxkb_ingest_concurrency": int(self.args.maxkb_ingest_concurrency),
                "resource_concurrency": "independent_resources_only",
                "client_concurrency": "thread_safe_artifact_http",
                "state_checkpoint": "resource-map.json after each resource transition and document readiness",
            }
        runtime = {
            "host": os.uname().nodename if hasattr(os, "uname") else "UNKNOWN",
            "docker_platform": value_from(self.env, "DOCKER_DEFAULT_PLATFORM", default="linux/arm64-or-emulated"),
            "cpu": os.cpu_count() or 0,
            "memory_gib": "UNKNOWN",
            "random_seed": str(self.package.manifest.get("random_seed", "UNKNOWN")),
            "qa_concurrency": int(self.args.qa_concurrency),
            "retrieval_concurrency": int(self.args.retrieval_concurrency),
        }
        if self.args.system == "fastgpt_local":
            runtime["fastgpt_ingest_concurrency"] = int(self.args.fastgpt_ingest_concurrency)
        elif self.args.system == "maxkb_local":
            runtime["maxkb_ingest_concurrency"] = int(self.args.maxkb_ingest_concurrency)
        return {
            "schema": "competitor-eval-run-start-v1",
            "run_id": self.args.run_id,
            "status_at_start": "not_started",
            "system_id": self.args.system,
            "dataset": self.package.dataset,
            "dataset_revision": self.package.revision,
            "split": self.package.split,
            "protocol_tag": self.package.protocol_tag,
            "condition": self.package.condition,
            "planned": {
                "files": len(self.package.documents),
                "pages": self.package.pages,
                "pages_known": self.package.pages_known,
                "questions": len(self.package.questions),
                "resources": 1 if self.package.scope == "global" else len(self.package.scope_groups),
                "repeats": int(self.args.repeats),
                "initial_attempts": len(self.package.questions) * int(self.args.repeats),
                "image_questions": self.package.image_question_count,
            },
            "data_hashes": self.package.hashes(),
            "system": {
                "version": value_from(self.env, "LOCAL_RAG_PLATFORM_VERSION", f"{self.args.system.upper()}_VERSION", default="UNKNOWN"),
                "deployment": "self_hosted",
                "image_digest": value_from(self.env, "LOCAL_IMAGE_DIGEST", f"{self.args.system.upper()}_IMAGE_DIGEST", default="UNKNOWN"),
                "platform": value_from(self.env, "DOCKER_DEFAULT_PLATFORM", default="linux/arm64-or-emulated"),
            },
            "pipeline": pipeline,
            "ingest_control": ingest_control,
            "metric_contract": _metric_contract(self.args, self.package),
            "denominator_contract": _denominator_contract(self.package, int(self.args.repeats)),
            "provider": provider,
            "runtime": runtime,
            "latency_boundary": {
                "ingest": "per document: immediately before upload/create request through searchable-ready terminal state; bounded wait time is retained",
                "retrieval": "monotonic clock immediately before the platform retrieval request through response decode",
                "generation": "monotonic clock immediately before the native QA request through response decode",
                "e2e": "monotonic clock immediately before the per-question stage request through response decode",
                "cold_or_warm": str(self.package.manifest.get("latency_mode", "UNKNOWN")),
            },
            "failure_policy": {
                "initial_denominator": "all planned initial attempts",
                "retry": "diagnostic only; never replaces an initial terminal row",
                "timeout_s": timeout,
                "max_retries": 0,
            },
            "artifact_policy": {
                "raw_http": True,
                "request_response_redacted": True,
                "ledger": "terminal-ledger.jsonl",
                "initial_ledger": "initial-ledger.jsonl",
                "config_snapshot": "start-record.json",
                "hash_sidecars": True,
                "app_secrets": "run-relative secrets/*.key files with mode 0600; resource-map stores paths only",
            },
            "resource_isolation": {
                "default": "fresh_dataset_or_knowledge_and_fresh_bound_native_app_per_resource",
                "same_run_resume": "resource-map only",
                "reuse_configured_resource": bool(
                    getattr(self.args, "reuse_configured_resource", False)
                    or _enabled(value_from(self.env, "COMPETITOR_EVAL_REUSE_CONFIGURED_RESOURCE"))
                ),
                "historical_app_ids_allowed": False,
            },
            "scope": {
                "requested": self.package.scope,
                "supported_by_contract": self.package.scope in self.contract.get("scope_modes", []),
                "verification": "package scope plus isolated resource-map key; final product scope remains a recorded capability claim",
            },
            "benchmark_scope": {
                "evaluation_order": CONTRACTS["benchmark_scope"]["evaluation_order"],
                "excluded_datasets": CONTRACTS["benchmark_scope"]["excluded_datasets"],
                "denominator_source": CONTRACTS["benchmark_scope"]["denominator_source"],
                "paper_full_corpus_required": False,
            },
            "preflight": {
                "service_ready": False,
                "provider_probe": False,
                "corpus_ready": False,
                "scope_verified": self.package.scope in self.contract.get("scope_modes", []),
                "network_performed": False,
            },
        }

    def _materialize_moi_artifacts(self) -> None:
        """Materialize a public-safe, isolated MOI config and canonical inputs."""

        if self.args.system != "moi_local":
            return
        moi_root = self.root / "moi"
        moi_root.mkdir(parents=True, exist_ok=True)
        (moi_root / ".gitignore").write_text("*.private.json\ncli-*.json\n", encoding="utf-8")
        embedding = self.profiles["maas"]
        generation = self.profiles["llm"]
        dsn = (
            value_from(self.env, "MOI_MATRIXONE_DSN", "MATRIXONE_DSN", "MOI_DSN")
            or "<set MOI_MATRIXONE_DSN>"
        )
        public_config = {
            "matrixone": {
                "dsn": "<set MOI_MATRIXONE_DSN>",
                "database": "",
                "vector_table": "",
            },
            "workspace_id": "",
            "matrixflow_root": value_from(self.env, "MOI_MATRIXFLOW_ROOT", default="../../../../matrixflow"),
            "chunk_size": 512,
            "chunk_overlap": 64,
            "section_size": 5,
            # MaaS embedding is remote; keep retrieval and QA serial for
            # benchmark comparability while allowing the ingest pipeline to
            # use the documented bounded parallelism for this provider.
            "embedding_batch_size": 64,
            "embedding_concurrency": 4,
            "embedding": {
                "api_key_env": "MAAS_API_KEY",
                "base_url": embedding.base_url,
                "dimension": embedding.embedding_dimension,
                "mode": "maas",
                "model": embedding.embedding_model,
            },
            "generation": {
                "enabled": True,
                "provider": generation.name,
                "base_url": generation.base_url,
                "model": generation.model,
                "api_key_env": generation.api_key_env,
                "thinking": str(THINKING_CONFIG.get("type", "disabled")),
                "include_page_images": False,
                "max_context_bytes": 100000,
                "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                "system_prompt": SHARED_PROMPT_TEXT,
                "prompt_version": SHARED_PROMPT_VERSION,
                "prompt_hash": SHARED_PROMPT_HASH,
                "prompt_status": "propagated:cli_native_system_prompt",
            },
        }
        for key, candidates in self.package.target_documents():
            resource_identifier = _moi_identifier(f"{self.args.run_id}_{key}")
            config_name = "config.json" if key == GLOBAL_RESOURCE else f"config-{_safe_name(key, 60)}.json"
            config_path = moi_root / config_name
            config_payload = deepcopy(public_config)
            config_payload["workspace_id"] = f"competitor-eval-{resource_identifier}"
            config_payload["matrixone"]["database"] = f"moi_eval_{resource_identifier}"
            config_payload["matrixone"]["vector_table"] = f"embedding_results_{resource_identifier}"
            json_dump(config_path, config_payload)
            private_config_path: Path | None = None
            if dsn != "<set MOI_MATRIXONE_DSN>":
                private_config_path = moi_root / config_name.replace(".json", ".private.json")
                private_config = deepcopy(config_payload)
                private_config["matrixone"]["dsn"] = dsn
                _atomic_write(
                    private_config_path,
                    (json.dumps(private_config, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
                    mode=0o600,
                )
                _write_hash(private_config_path)
            documents_path = moi_root / f"{_safe_name(key, 60)}-documents.jsonl"
            questions_path = moi_root / f"{_safe_name(key, 60)}-questions.jsonl"
            canonical = MoiAdapter.canonical_package_for(self.package, key, candidates)
            _write_jsonl(documents_path, canonical["documents"])
            _write_jsonl(questions_path, canonical["questions"])
            self._moi_artifacts[key] = {
                "config": str(config_path.relative_to(self.root)),
                "private_config": str(private_config_path.relative_to(self.root)) if private_config_path else None,
                "documents": str(documents_path.relative_to(self.root)),
                "questions": str(questions_path.relative_to(self.root)),
                "database": config_payload["matrixone"]["database"],
                "vector_table": config_payload["matrixone"]["vector_table"],
            }

    def _prepare_immutable_artifacts(self) -> None:
        start_path = self.root / "start-record.json"
        if start_path.exists():
            try:
                existing = json.loads(start_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise RunnerError(f"START_RECORD_INVALID: {start_path}") from exc
            expected = self._start_record()
            for key in ("run_id", "system_id", "dataset_revision", "scope", "data_hashes", "resource_isolation"):
                if existing.get(key) != expected.get(key):
                    raise RunnerError(f"START_RECORD_MISMATCH: {key}")
            self.start_record = existing
            if not start_path.with_suffix(start_path.suffix + ".sha256").exists():
                _write_hash(start_path)
        else:
            self.start_record = self._start_record()
            json_dump(start_path, self.start_record)

        initial_path = self.root / "initial-ledger.jsonl"
        if not initial_path.exists():
            rows = (
                {
                    "schema": "competitor-eval-initial-ledger-v1",
                    "attempt_id": f"{question.question_id}#repeat-{repeat}",
                    "question_id": question.question_id,
                    "repeat_id": repeat,
                    "stage": "initial",
                    "status": "not_started",
                    "media": question.media,
                    "planned_denominator": True,
                    **self.package.ledger_reference(question),
                }
                for question in self.package.questions
                for repeat in range(1, int(self.args.repeats) + 1)
            )
            _write_jsonl(initial_path, rows)
        elif not initial_path.with_suffix(initial_path.suffix + ".sha256").exists():
            _write_hash(initial_path)

        json_dump(self.root / "package-record.json", self.package.record())
        json_dump(self.root / "platform-contract.json", self.contract)
        self._materialize_sources()
        if not self.resource_map_path.exists():
            _write_json_map(self.resource_map_path, self._planned_resource_map())
        if not self.terminal_path.exists():
            self.terminal_path.touch()
            _write_hash(self.terminal_path)

    def _materialize_sources(self) -> None:
        source_dir = self.root / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        self._ingest_plans.clear()
        for resource_key, candidates in self.package.target_documents():
            unsupported: dict[str, str] = {}
            artifacts: list[Document] = []
            if self.package.ingest_representation == "candidate_markdown":
                usable = [document for document in candidates if document.ingest_role == "candidate_text" and document.content is not None]
                for document in candidates:
                    if document.ingest_role == "candidate_text" and document.content is None:
                        unsupported[document.document_id] = "CANDIDATE_INLINE_CONTENT_MISSING"
                if usable:
                    target = source_dir / f"{_safe_name(resource_key, 80)}-candidates.md"
                    with target.open("w", encoding="utf-8", newline="\n") as handle:
                        handle.write(f"# Competitor evaluation scope: {resource_key}\n\n")
                        for document in usable:
                            locator = {
                                "doc_id": document.document_id,
                                "scope_id": document.scope_id,
                                "page": document.marker_page,
                                "layout": document.marker_layout,
                                "quote": document.marker_quote,
                            }
                            marker = {key: value for key, value in locator.items() if value not in (None, "")}
                            handle.write(f"<!-- competitor-eval-candidate:{json.dumps(marker, ensure_ascii=False, sort_keys=True, separators=(',', ':'))} -->\n")
                            handle.write(f"## Candidate `{document.document_id}`\n\n{document.content or ''}\n\n")
                    artifact_hash = _sha256_file(target)
                    artifacts.append(
                        Document(
                            document_id=f"{resource_key}::candidate-markdown",
                            scope_id=resource_key,
                            ingest_role="materialized_candidate_markdown",
                            package_path=target,
                            content=None,
                            media=_media_tuple("markdown"),
                            declared_hash=artifact_hash,
                            artifact_path=target,
                        )
                    )
            else:
                selected = [document for document in candidates if document.ingest_role == "source_document"]
                seen_paths: set[Path] = set()
                for document in selected:
                    if document.package_path is None or not document.package_path.is_file():
                        unsupported[document.document_id] = "SOURCE_DOCUMENT_MISSING"
                        continue
                    resolved = document.package_path.resolve()
                    if resolved in seen_paths:
                        continue
                    seen_paths.add(resolved)
                    document.artifact_path = resolved
                    artifacts.append(document)
                if not artifacts:
                    for document in candidates:
                        unsupported.setdefault(document.document_id, "SOURCE_DOCUMENT_MISSING")
            self._ingest_plans[resource_key] = ResourceIngestPlan(
                representation=self.package.ingest_representation,
                artifacts=artifacts,
                unsupported_candidates=unsupported,
            )

    def ingest_plan(self, resource_key: str) -> ResourceIngestPlan:
        plan = self._ingest_plans.get(resource_key)
        if plan is None:
            raise RunnerError(f"RESOURCE_INGEST_PLAN_MISSING:{resource_key}")
        return plan

    def _planned_resource_map(self) -> dict[str, Any]:
        return {
            "schema": "competitor-eval-resource-map-v1",
            "run_id": self.args.run_id,
            "system_id": self.args.system,
            "scope": self.package.scope,
            "resources": {
                key: {
                    "status": "not_started",
                    "resource_key": key,
                    "document_count": len(documents),
                    **(
                        {"document_ids": [document.document_id for document in documents]}
                        if len(documents) <= 32
                        else {"document_ids_omitted": True}
                    ),
                    "document_ids_preview": [document.document_id for document in documents[:10]],
                    "candidate_set_sha256": f"sha256:{_sha256_canonical_records(document.document_id for document in documents)}",
                    "scope_id": key if self.package.scope == "document_local" else None,
                    "ingest_representation": self.package.ingest_representation,
                    "materialized_artifacts": [
                        {"id": artifact.document_id, "path": str(artifact.artifact_path), "sha256": artifact.source_hash}
                        for artifact in self.ingest_plan(key).artifacts
                    ],
                    "documents": {},
                }
                for key, documents in self.package.target_documents()
            },
        }

    def progress_emit(self, stage: str, message: str, **fields: Any) -> None:
        with self._state_lock:
            self.progress.emit(stage, message, run_id=self.args.run_id, system_id=self.args.system, **fields)

    def load_resources(self) -> dict[str, Any]:
        with self._state_lock:
            if not self.resource_map_path.exists():
                return self._planned_resource_map()
            try:
                payload = json.loads(self.resource_map_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return self._planned_resource_map()
            return payload if isinstance(payload, dict) else self._planned_resource_map()

    def save_resources(self, payload: dict[str, Any]) -> None:
        with self._state_lock:
            json_dump(self.resource_map_path, payload)
            json_dump(self.root / "resource-map.partial.json", payload)

    def terminal_keys(self) -> set[tuple[str, str, int]]:
        with self._state_lock:
            if self._terminal_keys_cache is None:
                self._terminal_rows_by_key.clear()
                retry_failed = value_from(
                    self.env,
                    "COMPETITOR_EVAL_RETRY_FAILED",
                    default="0",
                ).casefold() in {"1", "true", "yes", "on"}
                retry_unsupported = value_from(
                    self.env,
                    "COMPETITOR_EVAL_RETRY_UNSUPPORTED",
                    default="0",
                ).casefold() in {"1", "true", "yes", "on"}
                for row in _read_jsonl(self.terminal_path):
                    status = str(row.get("status"))
                    if retry_failed and status in {"FAILED", "TIMEOUT", "INTERRUPTED"}:
                        continue
                    if retry_unsupported and status == "UNSUPPORTED":
                        continue
                    if status not in TERMINAL_STATUSES:
                        continue
                    key = (str(row.get("stage")), str(row.get("question_id")), int(row.get("repeat_id", 1) or 1))
                    self._terminal_rows_by_key[key] = row
                self._terminal_keys_cache = set(self._terminal_rows_by_key)
            return self._terminal_keys_cache

    def append_terminal(self, stage: str, question: Question, status: str, *, repeat_id: int = 1, **fields: Any) -> dict[str, Any]:
        with self._state_lock:
            if status not in TERMINAL_STATUSES:
                raise RunnerError(f"TERMINAL_STATUS_INVALID: {status}")
            key = (stage, question.question_id, repeat_id)
            if key in self.terminal_keys():
                return self._terminal_rows_by_key[key]
            row = {
                "schema": "competitor-eval-terminal-ledger-v1",
                "run_id": self.args.run_id,
                "system_id": self.args.system,
                "stage": stage,
                "attempt_id": f"{question.question_id}#repeat-{repeat_id}",
                "question_id": question.question_id,
                "repeat_id": repeat_id,
                "status": status,
                "terminal": True,
                "planned_denominator": True,
                "question_media": question.media,
                "recorded_at": utc_now(),
                **self.package.ledger_reference(question),
                **fields,
            }
            _write_jsonl(self.terminal_path, [row], append=True)
            self.terminal_keys().add(key)
            self._terminal_rows_by_key[key] = row
            return row

    def stage_status(self, stage: str, status: str, **fields: Any) -> dict[str, Any]:
        payload = {"schema": "competitor-eval-stage-status-v1", "stage": stage, "status": status, "at": utc_now(), **fields}
        with self._state_lock:
            json_dump(self.root / f"{stage}-status.json", payload)
        return payload

    def _http(self, base_url: str | None = None, *, timeout: float | None = None) -> Any:
        normalized_base_url = (base_url or self.platform_base_url()).rstrip("/")
        with self._artifact_lock:
            client = self._http_clients.get(normalized_base_url)
            if client is None:
                client = ArtifactHTTP(
                    normalized_base_url,
                    self.root,
                    self.progress,
                    timeout=timeout
                    if timeout is not None
                    else max(float(self.args.query_timeout), float(self.args.qa_timeout), float(self.args.provider_timeout)),
                )
                self._http_clients[normalized_base_url] = self._protect_artifact_client(client)
            return client

    def _dify_native_model_discovery(self) -> dict[str, Any]:
        """Discover exact active Dify provider/model pairs; never guess IDs."""

        selected_llm = self.profiles["llm"]
        llm_provider = value_from(self.env, "DIFY_DEEPSEEK_LLM_PROVIDER")
        embedding_provider = value_from(self.env, "DIFY_MAAS_EMBEDDING_PROVIDER")
        dataset_key = value_from(self.env, "DIFY_LOCAL_DATASET_API_KEY", "DIFY_DATASET_API_KEY")
        result: dict[str, Any] = {
            "status": "BLOCKED",
            "ready": False,
            "selected": {
                "llm": {"provider": llm_provider or "NOT_CONFIGURED", "model": selected_llm.model},
                "embedding": {
                    "provider": embedding_provider or "NOT_CONFIGURED",
                    "model": self.profiles["maas"].embedding_model,
                },
            },
        }
        if not llm_provider or not embedding_provider:
            result["error"] = "DIFY_HYBRID_NATIVE_PROVIDER_SELECTION_MISSING"
            return result
        if not dataset_key:
            result["error"] = "DIFY_NATIVE_MODEL_DISCOVERY_DATASET_KEY_MISSING"
            return result

        def active_model(payload: Any, provider_name: str, model_name: str) -> dict[str, Any] | None:
            for provider_item in list_items(payload, ("data",)):
                if not isinstance(provider_item, Mapping):
                    continue
                discovered_provider = str(provider_item.get("provider") or provider_item.get("provider_name") or "")
                if discovered_provider != provider_name:
                    continue
                for model_item in list_items(provider_item.get("models", [])):
                    if not isinstance(model_item, Mapping):
                        continue
                    discovered_model = str(
                        model_item.get("model")
                        or model_item.get("model_name")
                        or model_item.get("name")
                        or ""
                    )
                    if discovered_model == model_name:
                        return {"provider": discovered_provider, "model": discovered_model, "active": True}
            return None

        try:
            client = self._http(self.platform_base_url())
            llm_payload = client.request(
                "GET",
                "/workspaces/current/models/model-types/llm",
                api_key=dataset_key,
                operation="dify-native-llm-discovery",
                timeout=self.args.provider_timeout,
            )
            embedding_payload = client.request(
                "GET",
                "/workspaces/current/models/model-types/text-embedding",
                api_key=dataset_key,
                operation="dify-native-embedding-discovery",
                timeout=self.args.provider_timeout,
            )
            llm = active_model(llm_payload, llm_provider, selected_llm.model)
            embedding = active_model(
                embedding_payload,
                embedding_provider,
                self.profiles["maas"].embedding_model,
            )
            result["llm"] = llm or {"provider": llm_provider, "model": selected_llm.model, "active": False}
            result["embedding"] = embedding or {
                "provider": embedding_provider,
                "model": self.profiles["maas"].embedding_model,
                "active": False,
            }
            if llm is None or embedding is None:
                result["error"] = "DIFY_NATIVE_SELECTED_MODEL_NOT_ACTIVE"
                return result
            result.update({"status": "READY", "ready": True})
            return result
        except Exception as exc:
            result["error"] = _error_text(exc)
            return result

    def platform_base_url(self) -> str:
        if self.args.system == "moi_local":
            return value_from(self.env, "MOI_BASE_URL", "MOI_API_URL", default="").rstrip("/")
        if self.args.system == "dify_local":
            return value_from(self.env, "DIFY_API_BASE_URL", default="http://127.0.0.1:8010/v1")
        if self.args.system == "fastgpt_local":
            return value_from(self.env, "FASTGPT_BASE_URL", default="http://127.0.0.1:3000")
        return value_from(self.env, "MAXKB_ADMIN_BASE_URL", default="http://127.0.0.1:8090/admin/api")

    def provider_probe(self) -> dict[str, Any]:
        def request_with_rate_limit_retry(client: Any, method: str, path: str, **kwargs: Any) -> Any:
            last: Exception | None = None
            for attempt in range(3):
                try:
                    return client.request(method, path, **kwargs)
                except Exception as exc:
                    last = exc
                    detail = _error_text(exc).casefold()
                    if attempt == 2 or ("429" not in detail and "overratelimit" not in detail and "rate limit" not in detail):
                        raise
                    time.sleep(2**attempt)
            raise last or ProviderUnavailable("PROVIDER_PROBE_FAILED")

        def probe_chat_profile(
            profile: ProviderProfile,
            *,
            operation_prefix: str,
            required: bool,
        ) -> dict[str, Any]:
            item: dict[str, Any] = {
                "provider": profile.public(),
                "required": required,
                "ready": False,
                "models": {},
            }
            if not required:
                item["status"] = "SKIPPED"
                item["reason"] = "NOT_REQUIRED_FOR_THIS_CONDITION"
                return item
            if not profile.api_key:
                item["error"] = f"{profile.name.upper()}_API_KEY_MISSING"
                return item
            try:
                client = self._http(profile.base_url)
                model_ids: set[str] = set()
                try:
                    models_payload = request_with_rate_limit_retry(
                        client,
                        "GET",
                        "/models",
                        api_key=profile.api_key,
                        operation=f"{operation_prefix}-models",
                        timeout=self.args.provider_timeout,
                    )
                    model_ids = {
                        str(entry.get("id"))
                        for entry in list_items(models_payload, ("data", "models"))
                        if isinstance(entry, dict) and entry.get("id")
                    }
                except Exception as exc:
                    item["models_error"] = _error_text(exc)
                item["models"] = {
                    "count": len(model_ids),
                    "llm_available": profile.model in model_ids if model_ids else None,
                }
                payload = request_with_rate_limit_retry(
                    client,
                    "POST",
                    "/chat/completions",
                    api_key=profile.api_key,
                    json_body={
                        "model": profile.model,
                        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                        "stream": False,
                        "temperature": 0,
                        "max_tokens": PROVIDER_CHAT_PROBE_MAX_TOKENS,
                        "thinking": dict(THINKING_CONFIG),
                    },
                    operation=f"{operation_prefix}-chat",
                    timeout=self.args.provider_timeout,
                )
                item["chat_non_empty"] = bool(answer_from(payload).strip())
                item["ready"] = bool(
                    item["chat_non_empty"]
                    and item["models"].get("llm_available") is not False
                )
            except Exception as exc:
                item["error"] = _error_text(exc)
            return item

        result: dict[str, Any] = {
            "schema": "competitor-eval-provider-probe-v1",
            "policy": "deepseek_text_maas_embedding",
            "thinking": dict(THINKING_CONFIG),
            "network_performed": False,
            "required": {
                "text_llm": self.profiles["llm"].name,
                "embedding": self.profiles["maas"].name,
                "image_model": self.package.image_question_count > 0,
            },
            "providers": {name: profile.public() for name, profile in self.profiles.items()},
            "embedding_provider": self.profiles["maas"].name,
            "results": {},
        }
        native_provider_required = self.args.system == "dify_local"
        native_provider_key = "DIFY_DEEPSEEK_LLM_PROVIDER" if native_provider_required else ""
        native_provider = value_from(self.env, native_provider_key) if native_provider_key else ""
        result["required"]["native_llm_provider"] = native_provider_key or "platform_selected_profile"
        result["native_llm_provider"] = native_provider or "NOT_CONFIGURED"
        if self.args.dry_run:
            if native_provider_required:
                result["native_model_discovery"] = {"status": "SKIPPED", "ready": False, "reason": "DRY_RUN"}
            result["status"] = "SKIPPED"
            result["reason"] = "DRY_RUN"
            json_dump(self.root / "providers" / "probe.json", result)
            return result
        if native_provider_required and not native_provider:
            result["results"]["native_llm"] = {
                "status": "BLOCKED",
                "ready": False,
                "error": "DIFY_DEEPSEEK_LLM_PROVIDER_MISSING",
                "provider": "NOT_CONFIGURED",
            }
            result["status"] = "BLOCKED"
            result["ready"] = False
            json_dump(self.root / "providers" / "probe.json", result)
            return result
        if native_provider_required:
            discovery = self._dify_native_model_discovery()
            result["native_model_discovery"] = discovery
            if not discovery.get("ready"):
                result["status"] = "BLOCKED"
                result["ready"] = False
                json_dump(self.root / "providers" / "probe.json", result)
                return result
        maas = self.profiles["maas"]
        text_llm = self.profiles["llm"]
        result["network_performed"] = True

        m_result: dict[str, Any] = {
            "provider": maas.public(),
            "ready": False,
        }
        if not maas.api_key:
            m_result["error"] = "MAAS_API_KEY_MISSING"
        else:
            try:
                client = self._http(maas.base_url)
                try:
                    models_payload = request_with_rate_limit_retry(client, "GET", "/models", api_key=maas.api_key, operation="provider-maas-models", timeout=self.args.provider_timeout)
                    model_ids = {
                        str(item.get("id"))
                        for item in list_items(models_payload, ("data", "models"))
                        if isinstance(item, dict) and item.get("id")
                    }
                    m_result["model_available"] = maas.embedding_model in model_ids if model_ids else None
                except Exception as exc:
                    m_result["models_error"] = _error_text(exc)
                embedding_payload = request_with_rate_limit_retry(
                    client,
                    "POST",
                    "/embeddings",
                    api_key=maas.api_key,
                    json_body={"model": maas.embedding_model, "input": ["MOI competitor provider health check"], "encoding_format": "float"},
                    operation="provider-maas-embeddings",
                    timeout=self.args.provider_timeout,
                )
                vectors = list_items(embedding_payload, ("data",))
                vector = vectors[0].get("embedding") if vectors and isinstance(vectors[0], dict) else first_value(embedding_payload, ("embedding",))
                dimension = len(vector) if isinstance(vector, list) else None
                m_result["embedding_dimension"] = dimension
                # MaaS model listings are useful diagnostics but are not a
                # complete capability registry for every account/region. The
                # direct embedding call below is authoritative for the frozen
                # embedding model. Text generation is probed independently on
                # the DeepSeek endpoint.
                m_result["ready"] = dimension == maas.embedding_dimension
            except Exception as exc:
                m_result["error"] = _error_text(exc)
        result["results"]["maas"] = m_result
        text_result = probe_chat_profile(
            text_llm,
            operation_prefix="provider-deepseek",
            required=True,
        )
        result["results"]["text_llm"] = text_result
        result["selected_text_llm"] = text_llm.public()
        result["ready"] = bool(
            m_result.get("ready")
            and text_result.get("ready")
            and (not native_provider_required or result.get("native_model_discovery", {}).get("ready"))
        )
        result["status"] = "READY" if result["ready"] else "BLOCKED"
        json_dump(self.root / "providers" / "probe.json", result)
        return result

    def _root_http(self, base_url: str) -> Any:
        parsed = urlsplit(base_url)
        path = parsed.path
        for marker in ("/v1", "/admin/api"):
            if path.endswith(marker):
                path = path[: -len(marker)]
        return self._http(
            urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/"), "", "")),
            timeout=self.args.service_timeout,
        )

    def service_readiness(self) -> dict[str, Any]:
        result: dict[str, Any] = {"system_id": self.args.system, "ready": False, "network_performed": False}
        if self.args.dry_run:
            result.update({"status": "SKIPPED", "reason": "DRY_RUN"})
            return result
        if self.args.system == "moi_local":
            command = value_from(self.env, "MOI_RUNNER_COMMAND")
            dsn = value_from(self.env, "MOI_MATRIXONE_DSN", "MATRIXONE_DSN", "MOI_DSN")
            if _enabled(value_from(self.env, "MOI_EVAL_FIXTURE")):
                result.update({"ready": True, "status": "FIXTURE", "adapter": "deterministic_fixture", "network_performed": False})
                return result
            if command and dsn:
                result.update({"ready": True, "status": "CLI_CONFIGURED", "adapter": "cli", "network_performed": False})
                return result
            result.update({
                "status": "BLOCKED",
                "error": "MOI_RUNNER_COMMAND_AND_MATRIXONE_DSN_REQUIRED",
                "action": "Set MOI_RUNNER_COMMAND and MOI_MATRIXONE_DSN, or use --dry-run/MOI_EVAL_FIXTURE=1.",
            })
            return result
        result["network_performed"] = True
        deadline = time.monotonic() + float(self.args.service_timeout)
        attempts = 0
        last_error = ""
        while time.monotonic() < deadline:
            attempts += 1
            remaining = max(1.0, deadline - time.monotonic())
            try:
                if self.args.system == "moi_local":
                    payload = self._http().request("GET", "/health", operation="service-readiness", timeout=remaining)
                elif self.args.system == "dify_local":
                    client = self._root_http(self.platform_base_url())
                    payload = client.request("GET", "/console/api/setup", operation="service-readiness", timeout=remaining)
                elif self.args.system == "fastgpt_local":
                    payload = self._http().request("GET", "/", operation="service-readiness", timeout=remaining)
                else:
                    client = self._root_http(self.platform_base_url())
                    payload = client.request("GET", "/admin/", operation="service-readiness", timeout=remaining)
                result.update({"ready": True, "status": "READY", "response_type": type(payload).__name__, "attempts": attempts})
                return result
            except Exception as exc:
                last_error = _error_text(exc)
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(float(self.args.poll_seconds), max(0.0, deadline - time.monotonic())))
        result.update({"status": "BLOCKED", "error": last_error, "attempts": attempts})
        return result

    def preflight(self) -> dict[str, Any]:
        if self._preflight_cache is not None:
            return self._preflight_cache
        probe = self.provider_probe()
        service = self.service_readiness()
        scope_ok = self.package.scope in self.contract.get("scope_modes", [])
        self._preflight_cache = {
            "schema": "competitor-eval-preflight-v1",
            "run_id": self.args.run_id,
            "system_id": self.args.system,
            "package_schema": PACKAGE_SCHEMA,
            "package_valid": True,
            "scope": self.package.scope,
            "scope_verified": scope_ok,
            "provider_probe": probe,
            "service_readiness": service,
            "network_performed": bool(probe.get("network_performed") or service.get("network_performed")),
            "ready": bool((self.args.dry_run or probe.get("ready")) and (self.args.dry_run or service.get("ready")) and scope_ok),
            "dry_run": bool(self.args.dry_run),
        }
        json_dump(self.root / "preflight.json", self._preflight_cache)
        self.stage_status("preflight", "DRY_RUN" if self.args.dry_run else ("READY" if self._preflight_cache["ready"] else "BLOCKED"), result=self._preflight_cache)
        self.progress_emit("preflight", "preflight finished", ready=self._preflight_cache["ready"])
        return self._preflight_cache

    def adapter(self) -> "BaseAdapter":
        if self.args.system == "moi_local":
            return MoiAdapter(self)
        if self.args.system == "dify_local":
            return DifyAdapter(self)
        if self.args.system == "fastgpt_local":
            return FastGPTAdapter(self)
        return MaxKBAdapter(self)

    def _media_supported(self, operation: str, media: Iterable[str]) -> tuple[bool, str | None]:
        supported = set(self.contract.get("media", {}).get(operation, []))
        missing = [value for value in media if value not in supported]
        return not missing, (f"MEDIA_UNSUPPORTED:{operation}:{','.join(missing)}" if missing else None)

    def question_scope_error(self, question: Question) -> str | None:
        if self.package.scope == "document_local" and len(question.scope_ids) != 1:
            return "DOCUMENT_LOCAL_QUESTION_MUST_RESOLVE_TO_ONE_SCOPE"
        return None

    def question_media_error(self, question: Question, operation: str) -> str | None:
        ok, reason = self._media_supported(operation, question.media)
        return None if ok else reason

    def resource_for(self, question: Question, resources: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None]:
        if self.package.scope == "global":
            resource = resources.get(GLOBAL_RESOURCE)
            return GLOBAL_RESOURCE, resource if isinstance(resource, dict) else None
        if len(question.scope_ids) != 1:
            return None, None
        scope_id = question.scope_ids[0]
        resource = resources.get(scope_id)
        return scope_id, resource if isinstance(resource, dict) else None

    def resource_unsupported_reason(self, question: Question, resource: dict[str, Any] | None) -> str | None:
        if not resource:
            return None
        document_states = resource.get("documents", {})
        if not isinstance(document_states, dict):
            return None
        count = resource.get("unsupported_document_count")
        if isinstance(count, int) and count > 0:
            return f"RESOURCE_DOCUMENTS_UNSUPPORTED:{count}"
        # Compatibility with an older same-run checkpoint.  This scan is over
        # one scope only and the upgraded result is persisted by ingest.
        unsupported_count = sum(
            1
            for state in document_states.values()
            if isinstance(state, dict) and state.get("status") == "unsupported"
        )
        return f"RESOURCE_DOCUMENTS_UNSUPPORTED:{unsupported_count}" if unsupported_count else None

    def _all_resource_blocked(self, reason: str) -> None:
        payload = self.load_resources()
        for key, resource in payload.setdefault("resources", {}).items():
            if str(resource.get("status")) == "not_started":
                resource.update({"status": "blocked", "error": reason})
        self.save_resources(payload)

    def ingest(self) -> dict[str, Any]:
        adapter = self.adapter()
        preflight = self.preflight()
        if self.args.dry_run:
            result = {"status": "DRY_RUN", "resources": self.load_resources()}
            self.stage_status("ingest", "DRY_RUN", result=result)
            return result
        if not preflight.get("ready"):
            self._all_resource_blocked("PREFLIGHT_NOT_READY")
            result = {"status": "BLOCKED", "reason": "PREFLIGHT_NOT_READY", "preflight": preflight}
            self.stage_status("ingest", "BLOCKED", result=result)
            return result
        result = adapter.ingest()
        self.stage_status("ingest", result.get("status", "PARTIAL"), result=result)
        return result

    def _terminal_blocked_for_stage(self, stage: str, reason: str) -> dict[str, Any]:
        statuses: Counter[str] = Counter()
        for question in self.package.questions:
            media_reason = self.question_media_error(question, stage if stage != "qa" else "native_qa")
            scope_reason = self.question_scope_error(question)
            if media_reason or scope_reason:
                status, detail = "UNSUPPORTED", media_reason or scope_reason
            else:
                status, detail = "BLOCKED", reason
            row = self.append_terminal(stage, question, status, error=detail, planned_denominator=True)
            statuses[str(row["status"])] += 1
        return {"status": "BLOCKED", "terminal_counts": dict(statuses), "reason": reason}

    def retrieval(self) -> dict[str, Any]:
        adapter = self.adapter()
        preflight = self.preflight()
        if self.args.dry_run:
            result = {"status": "DRY_RUN", "planned_questions": len(self.package.questions)}
            self.stage_status("retrieval", "DRY_RUN", result=result)
            return result
        if not preflight.get("ready"):
            result = self._terminal_blocked_for_stage("retrieval", "PREFLIGHT_NOT_READY")
            self.stage_status("retrieval", "BLOCKED", result=result)
            return result
        # A retrieval command resumes ingestion if the resource checkpoint is
        # still only planned.  This is bounded and reuses all ready resources.
        resources = self.load_resources().get("resources", {})
        if not any(str(resource.get("status")) in {"ready", "partial"} for resource in resources.values() if isinstance(resource, dict)):
            adapter.ingest()
            resources = self.load_resources().get("resources", {})
        terminal_counts: Counter[str] = Counter()
        latencies: list[float] = []
        rows: list[dict[str, Any]] = []
        existing = self.terminal_keys()
        work_items = [
            (question, repeat_id)
            for question in self.package.questions
            for repeat_id in range(1, int(self.args.repeats) + 1)
            if ("retrieval", question.question_id, repeat_id) not in existing
        ]

        def retrieve_one(item: tuple[Question, int]) -> tuple[str, dict[str, Any], float | None]:
            question, repeat_id = item
            media_reason = self.question_media_error(question, "retrieval")
            scope_reason = self.question_scope_error(question)
            key, resource = self.resource_for(question, resources)
            if media_reason or scope_reason:
                return "UNSUPPORTED", {"error": media_reason or scope_reason}, None
            resource_media_reason = self.resource_unsupported_reason(question, resource)
            if resource_media_reason:
                return "UNSUPPORTED", {"error": resource_media_reason, "resource_key": key}, None
            if resource is None or str(resource.get("status")) not in {"ready", "partial"}:
                return "BLOCKED", {"error": "RESOURCE_NOT_READY", "resource_key": key}, None
            started = time.monotonic()
            try:
                operation = adapter.retrieve(question, resource)
                latency = (time.monotonic() - started) * 1000
                hits = operation.get("hits", []) if isinstance(operation, dict) else []
                contract = str(operation.get("contract", "public_direct_retrieval")) if isinstance(operation, dict) else "public_direct_retrieval"
                scores = _retrieval_metrics(question, hits, int(self.args.top_k))
                if contract == "diagnostic_admin_contract":
                    # MaxKB 2.10.4 exposes admin hit_test as its supported
                    # retrieval surface; it is diagnostic rather than public,
                    # but the returned hits are still evaluable evidence.
                    status = "SUCCESS" if hits else "EMPTY"
                    fields = {
                        "retrieval_contract": contract,
                        "hits": hits,
                        "hit_count": len(hits),
                        "metrics": scores,
                        "latency_ms": round(latency, 3),
                        "resource_key": key,
                    }
                else:
                    status = "SUCCESS" if hits else "EMPTY"
                    fields = {
                        "retrieval_contract": contract,
                        "hits": hits,
                        "hit_count": len(hits),
                        "metrics": scores,
                        "latency_ms": round(latency, 3),
                        "resource_key": key,
                    }
                return status, fields, latency
            except Exception as exc:
                status = _exception_status(exc)
                return status, {"error": _error_text(exc), "resource_key": key, "latency_ms": round((time.monotonic() - started) * 1000, 3)}, None

        with ThreadPoolExecutor(
            max_workers=int(self.args.retrieval_concurrency),
            thread_name_prefix="competitor-retrieval",
        ) as executor:
            outcomes = executor.map(retrieve_one, work_items)
            for (question, repeat_id), (status, fields, latency) in zip(work_items, outcomes):
                row = self.append_terminal("retrieval", question, status, repeat_id=repeat_id, **fields)
                terminal_counts[row["status"]] += 1
                rows.append(row)
                if latency is not None:
                    latencies.append(latency)
        metrics = self._stage_metrics("retrieval", rows, latencies)
        result = {"status": "SUCCESS" if not terminal_counts.get("FAILED") and not terminal_counts.get("BLOCKED") else "PARTIAL", "terminal_counts": dict(terminal_counts), "metrics": metrics}
        json_dump(self.root / "retrieval-metrics.json", metrics)
        self.stage_status("retrieval", result["status"], result=result)
        return result

    def _qa_attempt(
        self,
        question: Question,
        repeat_id: int,
        key: str | None,
        resource: dict[str, Any] | None,
    ) -> tuple[str, dict[str, Any], float | None]:
        session_id = _qa_session_id(question.question_id, repeat_id)
        media_reason = self.question_media_error(question, "native_qa")
        scope_reason = self.question_scope_error(question)
        if media_reason or scope_reason:
            return "UNSUPPORTED", {"error": media_reason or scope_reason, "session_id": session_id}, None
        resource_media_reason = self.resource_unsupported_reason(question, resource)
        if resource_media_reason:
            return "UNSUPPORTED", {"error": resource_media_reason, "resource_key": key, "session_id": session_id}, None
        if resource is None or str(resource.get("status")) not in {"ready", "partial"}:
            return "BLOCKED", {"error": "RESOURCE_NOT_READY", "resource_key": key, "session_id": session_id}, None

        started = time.monotonic()
        try:
            adapter = self.adapter()
            operation = adapter.qa(_qa_session_question(deepcopy(question), repeat_id), deepcopy(resource))
            latency = (time.monotonic() - started) * 1000
            answer = str(operation.get("answer", "") if isinstance(operation, dict) else "")
            metrics = _answer_metrics(answer, question.answer)
            status = "SUCCESS" if answer.strip() else "EMPTY"
            return status, {
                "answer": answer,
                "answer_metrics": metrics,
                "tdas": {"status": "UNSUPPORTED", "reason": "NO_FROZEN_JUDGE"},
                "qa_contract": operation.get("contract") if isinstance(operation, dict) else "native_qa",
                "generation_provider": operation.get("generation_provider") if isinstance(operation, dict) else None,
                "generation_model": operation.get("generation_model") if isinstance(operation, dict) else None,
                "raw_answer_present": bool(answer),
                "latency_ms": round(latency, 3),
                "resource_key": key,
                "session_id": session_id,
            }, latency
        except Exception as exc:
            return _exception_status(exc), {
                "error": _error_text(exc),
                "resource_key": key,
                "latency_ms": round((time.monotonic() - started) * 1000, 3),
                "session_id": session_id,
            }, None

    def qa(self) -> dict[str, Any]:
        adapter = self.adapter()
        preflight = self.preflight()
        if self.args.dry_run:
            result = {"status": "DRY_RUN", "planned_questions": len(self.package.questions)}
            self.stage_status("qa", "DRY_RUN", result=result)
            return result
        if not preflight.get("ready"):
            result = self._terminal_blocked_for_stage("qa", "PREFLIGHT_NOT_READY")
            self.stage_status("qa", "BLOCKED", result=result)
            return result
        resources = self.load_resources().get("resources", {})
        # QA-only campaigns still require every planned resource to reach a
        # terminal ingest state.  Resume pending/failed scopes even when some
        # sibling scopes are already ready; otherwise those questions would be
        # incorrectly recorded as RESOURCE_NOT_READY without a retry.
        if any(
            str(resource.get("status")) not in {"ready", "partial", "unsupported"}
            for resource in resources.values()
            if isinstance(resource, dict)
        ):
            adapter.ingest()
            resources = self.load_resources().get("resources", {})

        terminal_counts: Counter[str] = Counter()
        latencies: list[float] = []
        rows: list[dict[str, Any]] = []
        existing = self.terminal_keys()
        work_items = [
            (question, repeat_id, *self.resource_for(question, resources))
            for question in self.package.questions
            for repeat_id in range(1, int(self.args.repeats) + 1)
            if ("qa", question.question_id, repeat_id) not in existing
        ]
        with ThreadPoolExecutor(
            max_workers=int(self.args.qa_concurrency),
            thread_name_prefix="competitor-qa",
        ) as executor:
            outcomes = executor.map(lambda item: self._qa_attempt(*item), work_items)
            for (question, repeat_id, key, _resource), (status, fields, latency) in zip(work_items, outcomes):
                row = self.append_terminal("qa", question, status, repeat_id=repeat_id, **fields)
                terminal_counts[row["status"]] += 1
                rows.append(row)
                if latency is not None:
                    latencies.append(latency)
        metrics = self._stage_metrics("qa", rows, latencies)
        non_success = ("FAILED", "BLOCKED", "TIMEOUT", "INTERRUPTED")
        status_counts = metrics.get("status_counts", {})
        result = {
            "status": "PARTIAL" if any(status_counts.get(status) for status in non_success) else "SUCCESS",
            "terminal_counts": dict(terminal_counts),
            "metrics": metrics,
        }
        json_dump(self.root / "qa-metrics.json", metrics)
        self.stage_status("qa", result["status"], result=result)
        return result

    def _stage_metrics(self, stage: str, rows: list[dict[str, Any]], latencies: list[float]) -> dict[str, Any]:
        latest_rows: dict[tuple[str, int], dict[str, Any]] = {}
        for row in _read_jsonl(self.terminal_path):
            if row.get("stage") != stage:
                continue
            key = (str(row.get("question_id")), int(row.get("repeat_id", 1) or 1))
            latest_rows[key] = row
        all_rows = list(latest_rows.values())
        planned = len(self.package.questions) * int(self.args.repeats)
        statuses = Counter(str(row.get("status")) for row in all_rows)
        valid_rows = [row for row in all_rows if row.get("status") in {"SUCCESS", "EMPTY"}]
        all_latencies = [
            float(row["latency_ms"])
            for row in all_rows
            if isinstance(row.get("latency_ms"), (int, float))
        ]
        metric: dict[str, Any] = {
            "schema": "competitor-eval-stage-metrics-v1",
            "stage": stage,
            "planned_n": planned,
            "terminal_n": len(all_rows),
            "valid_n": len(valid_rows),
            "failed_n": sum(statuses.get(status, 0) for status in ("FAILED", "BLOCKED", "TIMEOUT", "INTERRUPTED")),
            "unsupported_n": statuses.get("UNSUPPORTED", 0),
            "status_counts": dict(statuses),
            "latency_ms_p50": _percentile(all_latencies or latencies, 0.50),
            "latency_ms_p95": _percentile(all_latencies or latencies, 0.95),
            "denominator_policy": "all planned question/repeat keys; latest terminal row is used when a failed attempt is retried",
        }
        if stage == "retrieval":
            for cutoff in DEFAULT_TOP_K:
                values = [
                    float(row.get("metrics", {}).get("evidence_recall", 0.0))
                    for row in valid_rows
                    if row.get("retrieval_contract") in {"public_direct_retrieval", "diagnostic_admin_contract"}
                    and row.get("metrics", {}).get("evidence_recall") is not None
                ]
                metric[f"evidence_recall_at_{cutoff}"] = sum(values) / len(values) if values else None
            reciprocal = [
                float(row.get("metrics", {}).get("mrr", 0.0))
                for row in valid_rows
                if row.get("retrieval_contract") in {"public_direct_retrieval", "diagnostic_admin_contract"}
                and row.get("metrics", {}).get("mrr") is not None
            ]
            metric["mrr"] = sum(reciprocal) / len(reciprocal) if reciprocal else None
            metric["public_retrieval_contract"] = "diagnostic_admin_contract" if self.args.system == "maxkb_local" else "public_direct_retrieval"
        else:
            answer_values = [row.get("answer_metrics", {}) for row in valid_rows]
            metric["answer_non_empty_rate"] = sum(bool(value.get("answer_non_empty")) for value in answer_values) / planned if planned else None
            metric["answer_contains_gold_rate"] = sum(bool(value.get("contains_gold")) for value in answer_values) / planned if planned else None
            em_values = [float(bool(value.get("normalized_em"))) for value in answer_values]
            f1_values = [float(value["token_f1"]) for value in answer_values if value.get("token_f1") is not None]
            metric["normalized_em"] = sum(em_values) / planned if planned else None
            metric["token_f1"] = sum(f1_values) / planned if planned and f1_values else None
            metric["tdas"] = None
            metric["tdas_reason"] = "NO_FROZEN_JUDGE"
            if self.args.system == "maxkb_local":
                metric["qa_contract"] = MAXKB_EXTERNAL_QA_CONTRACT
                metric["comparability"] = MAXKB_QA_COMPARABILITY
            else:
                metric["qa_contract"] = "native_chat"
        return metric

    def run(self) -> dict[str, Any]:
        command = self.args.command
        preflight = self.preflight()
        result: dict[str, Any] = {"run_id": self.args.run_id, "system_id": self.args.system, "command": command, "preflight": preflight}
        if command == "preflight":
            return result
        if command in ("ingest", "all"):
            result["ingest"] = self.ingest()
        if command in ("retrieval", "all"):
            result["retrieval"] = self.retrieval()
        if command in ("qa", "all"):
            result["qa"] = self.qa()
        # Retrieval or QA may resume an interrupted ingest and make all
        # resources ready. Reconcile the earlier stage result with the durable
        # resource map so a recovered transient error does not stop a campaign.
        if command == "all" and isinstance(result.get("ingest"), dict):
            resources = self.load_resources().get("resources", {})
            states = [
                str(resource.get("status", ""))
                for resource in resources.values()
                if isinstance(resource, dict)
            ]
            if states and all(state in {"ready", "partial", "unsupported"} for state in states):
                result["ingest"] = {
                    **result["ingest"],
                    "status": "SUCCESS",
                    "resource_counts": dict(Counter(state.upper() for state in states)),
                    "reconciled_from_resource_map": True,
                }
        result["status"] = "DRY_RUN" if self.args.dry_run else ("BLOCKED" if not preflight.get("ready") else "SUCCESS")
        if any(isinstance(value, dict) and value.get("status") == "PARTIAL" for value in result.values()):
            result["status"] = "PARTIAL"
        json_dump(self.root / "summary.json", result)
        self.progress_emit("complete", "runner finished", status=result["status"])
        return result


class BaseAdapter:
    def __init__(self, context: RunnerContext):
        self.context = context
        self.args = context.args
        self.package = context.package
        self.env = context.env
        self.client = context._http()
        self.contract = context.contract
        self.reuse_configured_resource = bool(
            getattr(self.args, "reuse_configured_resource", False)
            or _enabled(value_from(self.env, "COMPETITOR_EVAL_REUSE_CONFIGURED_RESOURCE"))
        )

    def _supported(self, operation: str, media: Iterable[str]) -> None:
        supported = set(self.contract.get("media", {}).get(operation, []))
        missing = [value for value in media if value not in supported]
        if missing:
            raise ContractUnsupported(f"MEDIA_UNSUPPORTED:{operation}:{','.join(missing)}")

    @staticmethod
    def _unwrap_fastgpt(payload: Any) -> Any:
        if isinstance(payload, dict) and payload.get("code") not in (None, 200):
            raise EvalError(f"FASTGPT_API_ERROR:{payload.get('code')}:{payload.get('message', '')}")
        return payload.get("data", payload) if isinstance(payload, dict) else payload

    def _id(self, payload: Any, names: tuple[str, ...] = ("id", "datasetId", "dataset_id", "collectionId", "collection_id", "document_id")) -> str:
        if isinstance(payload, (str, int)) and str(payload).strip():
            return str(payload)
        value = first_value(payload, names)
        identifiers = _ids(value)
        return identifiers[0] if identifiers else ""

    def _resources_payload(self) -> dict[str, Any]:
        return self.context.load_resources()

    def _save_resource(self, key: str, resource: dict[str, Any]) -> None:
        # Keep the read/merge/write sequence inside the same lock.  A worker
        # must never serialize a stale snapshot over a sibling resource.
        with self.context._state_lock:
            payload = self._resources_payload()
            payload.setdefault("resources", {})[key] = resource
            payload["updated_at"] = utc_now()
            self.context.save_resources(payload)

    def _target_is_usable(self, resource: dict[str, Any] | None) -> bool:
        return bool(resource and str(resource.get("status")) in {"ready", "partial"} and resource.get("resource_id"))

    def _target_resources(self) -> list[tuple[str, list[Document]]]:
        return self.package.target_documents()

    def _ingest_selection(
        self,
        key: str,
        candidates: list[Document],
        resource: dict[str, Any],
    ) -> tuple[list[Document], dict[str, str]]:
        plan = self.context.ingest_plan(key)
        unsupported = dict(plan.unsupported_candidates)
        artifact_media_errors = [
            artifact
            for artifact in plan.artifacts
            if any(media not in self.contract["media"]["ingest"] for media in artifact.media)
        ]
        if (
            plan.representation == "source_document"
            and self.args.system in {"fastgpt_local", "maxkb_local"}
            and any("pdf" in artifact.media for artifact in plan.artifacts)
        ):
            artifact_media_errors = list(plan.artifacts)
            for candidate in candidates:
                unsupported.setdefault(candidate.document_id, "SOURCE_PDF_NATIVE_CONTRACT_UNSUPPORTED")
        if artifact_media_errors:
            reason = "MEDIA_UNSUPPORTED:ingest:" + ",".join(
                sorted({media for artifact in artifact_media_errors for media in artifact.media})
            )
            for candidate in candidates:
                unsupported.setdefault(candidate.document_id, reason)
        resource["ingest_representation"] = plan.representation
        resource["materialized_artifacts"] = [
            {"id": artifact.document_id, "path": str(artifact.artifact_path), "sha256": artifact.source_hash}
            for artifact in plan.artifacts
        ]
        resource.setdefault("documents", {})
        for candidate in candidates:
            if candidate.document_id in unsupported:
                resource["documents"][candidate.document_id] = {
                    "status": "unsupported",
                    "error": unsupported[candidate.document_id],
                    "sha256": candidate.source_hash,
                }
        resource["unsupported_document_count"] = len(unsupported)
        resource["unsupported_document_ids_preview"] = list(unsupported)[:10]
        artifacts = [] if artifact_media_errors else list(plan.artifacts)
        return artifacts, unsupported

    @staticmethod
    def _mark_candidates_ready(resource: dict[str, Any], candidates: list[Document], unsupported: Mapping[str, str]) -> None:
        resource["ready_document_count"] = len(candidates) - len(unsupported)

    def _existing(self, key: str) -> dict[str, Any] | None:
        value = self._resources_payload().get("resources", {}).get(key)
        return value if isinstance(value, dict) else None

    def _configured_resource_allowed(self, key: str, configured_id: str) -> bool:
        return bool(key == GLOBAL_RESOURCE and configured_id and self.reuse_configured_resource)

    def _native_app_resumable(self, resource: Mapping[str, Any]) -> bool:
        native = resource.get("native_app")
        if not isinstance(native, Mapping) or native.get("status") != "ready":
            return False
        selected_llm = self.context.profiles["llm"]
        if str(native.get("model") or "") != selected_llm.model:
            return False
        recorded_provider = str(native.get("provider") or "")
        if recorded_provider and recorded_provider != selected_llm.name:
            return False
        if not resource.get("app_id") or not resource.get("app_key_secret_path"):
            return False
        try:
            self.context.read_secret(resource["app_key_secret_path"])
        except ProviderUnavailable:
            return False
        return True

    def _record_native_setup_failure(self, resource: dict[str, Any], exc: BaseException) -> None:
        status = _native_setup_status(exc)
        resource["native_app"] = {
            "status": status,
            "error": _error_text(exc),
            "bound_resource_id": resource.get("resource_id"),
        }
        resource.pop("app_id", None)
        resource.pop("app_key_secret_path", None)

    def service_ready(self) -> bool:
        return bool(self.context.service_readiness().get("ready"))

    def ingest(self) -> dict[str, Any]:
        raise NotImplementedError

    def retrieve(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def qa(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _question_message(self, question: Question) -> Any:
        if not question.image_paths:
            return question.text
        content: list[dict[str, Any]] = [{"type": "text", "text": question.text}]
        for image_path in question.image_paths:
            mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        return content


class MoiAdapter(BaseAdapter):
    """Deterministic adapter for the canonical MOI evaluation package.

    The local MOI service is an external dependency and does not have a
    stable native API in this runner yet.  Dry-runs and an explicit
    ``MOI_EVAL_FIXTURE=1`` therefore use the package itself as a deterministic
    retrieval/QA fixture.  A live run without that opt-in records a blocked
    resource instead of scanning source directories or fabricating a result.
    """

    def __init__(self, context: RunnerContext):
        self.context = context
        self.args = context.args
        self.package = context.package
        self.env = context.env
        self.client = None
        self.contract = context.contract
        self.reuse_configured_resource = False
        # A fixture is an artifact-only dry-run aid.  Non-dry-run fixture use
        # is rejected by RunnerContext before an adapter can be constructed.
        self.fixture_enabled = bool(self.args.dry_run)

    @staticmethod
    def canonical_question(question: Question, *, source_dataset: str) -> dict[str, Any]:
        raw = dict(question.raw)
        relevant_documents = list(question.document_ids)
        if not relevant_documents:
            for key in ("gold_doc_ids", "relevant_documents", "document_ids"):
                value = raw.get(key)
                if isinstance(value, str):
                    value = [value]
                if isinstance(value, list):
                    relevant_documents = [str(item) for item in value if item]
                    if relevant_documents:
                        break
        evidence_value = raw.get("gold_evidence", raw.get("evidence", []))
        if isinstance(evidence_value, str):
            evidence_value = [evidence_value]
        evidence: list[str] = []
        if isinstance(evidence_value, list):
            for item in evidence_value:
                if isinstance(item, Mapping):
                    value = item.get("evidence") or item.get("text") or item.get("quote")
                    if value:
                        evidence.append(str(value))
                elif item:
                    evidence.append(str(item))
        return {
            "id": question.question_id,
            "question": question.text,
            "relevant_documents": relevant_documents,
            "relevant_evidence": evidence,
            "reference_answer": question.answer,
            "source_dataset": raw.get("source_dataset", source_dataset),
            "question_type": raw.get("question_type"),
            "answerability": raw.get("answerability", raw.get("answerable")),
        }

    @staticmethod
    def canonical_document(document: Document) -> dict[str, Any]:
        content = document.content
        candidate_paths = [document.artifact_path, document.package_path]
        if content is None:
            for candidate in candidate_paths:
                if candidate is not None and candidate.is_file():
                    content = candidate.read_text(encoding="utf-8", errors="replace")
                    break
        metadata = dict(document.raw.get("metadata", {})) if isinstance(document.raw.get("metadata"), Mapping) else {}
        # MatrixFlow's production parsed-document expansion carries source
        # identity through metadata.file_id.  Keep the public benchmark ID as
        # that identity so the Go product ingest path can preserve source
        # attribution without depending on a private path or source record.
        metadata.setdefault("file_id", document.document_id)
        metadata.setdefault("file_name", document.document_id)
        return {
            "id": document.document_id,
            "scope_id": document.scope_id,
            "media": list(document.media),
            "content": content or "",
            "sha256": document.source_hash,
            "metadata": metadata,
        }

    @staticmethod
    def canonical_package_for(package: EvalPackage, key: str, documents: list[Document]) -> dict[str, Any]:
        questions = [
            MoiAdapter.canonical_question(question, source_dataset=package.dataset)
            for question in package.questions
            if package.scope == "global" or key in question.scope_ids
        ]
        return {
            "schema": PACKAGE_SCHEMA,
            "dataset": package.dataset,
            "revision": package.revision,
            "condition": package.condition,
            "scope": package.scope,
            "resource_key": key,
            "documents": [MoiAdapter.canonical_document(document) for document in documents],
            "questions": questions,
        }

    def canonical_package(self, key: str, documents: list[Document]) -> dict[str, Any]:
        return self.canonical_package_for(self.package, key, documents)

    def _fixture_resource(self, key: str, candidates: list[Document]) -> dict[str, Any]:
        package = self.canonical_package(key, candidates)
        return {
            "status": "ready",
            "ready": True,
            "resource_key": key,
            "resource_id": f"moi-fixture:{self.args.run_id}:{_safe_name(key)}",
            "contract": "moi_canonical_package_fixture",
            "ingest_representation": "canonical_package",
            "canonical_package": package,
            "documents": {
                document.document_id: {
                    "status": "ready",
                    "resource_id": document.document_id,
                    "sha256": document.source_hash,
                }
                for document in candidates
            },
            "ready_document_count": len(candidates),
            "unsupported_document_count": 0,
            "native_app": {
                "status": "ready",
                "contract": "moi_canonical_package_fixture",
                "bound_resource_id": package["resource_key"],
                "model": self.context.profiles["llm"].model,
                "provider": self.context.profiles["llm"].name,
                "thinking": dict(THINKING_CONFIG),
            },
        }

    def _cli_command(self, stage: str, key: str, artifact: Mapping[str, Any]) -> tuple[list[str], Path]:
        template = value_from(self.env, "MOI_RUNNER_COMMAND")
        if not template:
            raise ProviderUnavailable("MOI_RUNNER_COMMAND_MISSING")
        try:
            command = shlex.split(template)
        except ValueError as exc:
            raise ProviderUnavailable("MOI_RUNNER_COMMAND_INVALID") from exc
        if not command:
            raise ProviderUnavailable("MOI_RUNNER_COMMAND_MISSING")
        config_path = (self.context.root / str(artifact.get("private_config") or artifact["config"])).resolve()
        documents_path = (self.context.root / str(artifact["documents"])).resolve()
        questions_path = (self.context.root / str(artifact["questions"])).resolve()
        cli_run_root = self.context.root / "moi" / f"cli-output-{_safe_name(key, 60)}"
        values = {
            "stage": stage,
            "config": str(config_path),
            "documents": str(documents_path),
            "dataset": str(questions_path),
            "run": str(cli_run_root),
            "package": str(self.context.root / "moi"),
        }
        if any("{" in token or "}" in token for token in command):
            try:
                command = [token.format(**values) for token in command]
            except (KeyError, ValueError) as exc:
                raise ProviderUnavailable("MOI_RUNNER_COMMAND_PLACEHOLDER_INVALID") from exc
        else:
            native_stage = "pipeline" if stage in {"all", "ingest"} else "run"
            command.extend([native_stage, "--config", str(config_path), "--run", str(cli_run_root)])
            if native_stage == "pipeline":
                command.extend(["--documents", str(documents_path), "--dataset", str(questions_path)])
            else:
                command.extend(["--dataset", str(questions_path)])
        return command, cli_run_root

    def _run_cli(self, stage: str, key: str, artifact: Mapping[str, Any]) -> dict[str, Any]:
        command, cli_run_root = self._cli_command(stage, key, artifact)
        cwd = Path(value_from(self.env, "MOI_RUNNER_CWD", default=str(ROOT))).expanduser()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=dict(self.env),
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            raise ProviderUnavailable(f"MOI_CLI_EXECUTION_FAILED:{type(exc).__name__}") from exc
        result_files = sorted(cli_run_root.rglob("results.jsonl")) if cli_run_root.exists() else []
        result_file = str(result_files[-1].relative_to(self.context.root)) if result_files else None
        payload = {
            "status": "READY" if completed.returncode == 0 else "BLOCKED",
            "returncode": int(completed.returncode),
            "command": command,
            "result_file": result_file,
            "stdout": redact_text(completed.stdout or "")[-4000:],
            "stderr": redact_text(completed.stderr or "")[-4000:],
        }
        json_dump(
            self.context.root / "moi" / f"cli-{_safe_name(key, 60)}-{stage}.json",
            payload,
        )
        return payload

    def _cli_result(self, resource: Mapping[str, Any], question: Question) -> dict[str, Any]:
        cli = resource.get("cli")
        result_file = cli.get("result_file") if isinstance(cli, Mapping) else None
        if not result_file:
            raise ProviderUnavailable("MOI_CLI_RESULT_FILE_MISSING")
        path = (self.context.root / str(result_file)).resolve()
        if self.context.root != path and self.context.root not in path.parents:
            raise ProviderUnavailable("MOI_CLI_RESULT_FILE_OUTSIDE_RUN")
        with self.context._state_lock:
            offsets = self.context._moi_result_offsets.get(path)
            if offsets is None:
                offsets = {}
                with path.open("rb") as handle:
                    while True:
                        offset = handle.tell()
                        line = handle.readline()
                        if not line:
                            break
                        row = json.loads(line)
                        case = row.get("case")
                        case_id = case.get("id") if isinstance(case, Mapping) else row.get("question_id")
                        if case_id is not None:
                            offsets.setdefault(str(case_id), offset)
                self.context._moi_result_offsets[path] = offsets

        requested_ids = (question.question_id, question.question_id.split("#repeat-", 1)[0])
        for requested_id in requested_ids:
            offset = offsets.get(requested_id)
            if offset is None:
                continue
            with path.open("rb") as handle:
                handle.seek(offset)
                row = json.loads(handle.readline())
            if str(row.get("status", "")).casefold() == "failed":
                raise ProviderUnavailable(f"MOI_CLI_ATTEMPT_FAILED:{_error_text(EvalError(str(row.get('error') or 'UNKNOWN')))}")
            return row
        raise ProviderUnavailable(f"MOI_CLI_RESULT_NOT_FOUND:{question.question_id}")

    def ingest(self) -> dict[str, Any]:
        resources = self.context.load_resources()
        counts: Counter[str] = Counter()
        for key, candidates in self.package.target_documents():
            existing = resources.setdefault("resources", {}).setdefault(key, {})
            if self.fixture_enabled:
                resource = {**existing, **self._fixture_resource(key, candidates)}
                resources["resources"][key] = resource
                counts["READY"] += 1
            else:
                artifact = self.context._moi_artifacts.get(key)
                if not artifact:
                    resource = {**existing, "status": "blocked", "ready": False, "error": "MOI_ARTIFACT_MISSING"}
                    counts["BLOCKED"] += 1
                    resources["resources"][key] = resource
                    continue
                try:
                    cli = self._run_cli("ingest", key, artifact)
                    if cli["returncode"] == 0:
                        resource = {
                            **existing,
                            "status": "ready",
                            "ready": True,
                            "resource_key": key,
                            "resource_id": f"moi-cli:{self.args.run_id}:{_safe_name(key)}",
                            "contract": "moi_cli_native",
                            "ingest_representation": "canonical_package",
                            "config": artifact["config"],
                            "database": artifact["database"],
                            "vector_table": artifact["vector_table"],
                            "cli": cli,
                            "native_app": {
                                "status": "ready",
                                "contract": "moi_cli_native",
                                "model": self.context.profiles["llm"].model,
                                "provider": self.context.profiles["llm"].name,
                                "thinking": dict(THINKING_CONFIG),
                            },
                        }
                        counts["READY"] += 1
                    else:
                        resource = {**existing, "status": "blocked", "ready": False, "error": "MOI_CLI_FAILED", "cli": cli}
                        counts["BLOCKED"] += 1
                except Exception as exc:
                    resource = {**existing, "status": "blocked", "ready": False, "error": _error_text(exc), "contract": "moi_cli_native"}
                    counts["BLOCKED"] += 1
                resources["resources"][key] = resource
        resources["updated_at"] = utc_now()
        self.context.save_resources(resources)
        return {
            "status": "SUCCESS" if counts and not counts.get("BLOCKED") else "BLOCKED",
            "resource_counts": dict(counts),
            "canonical_package": self.fixture_enabled,
        }

    def retrieve(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        self._supported("retrieval", question.media)
        if not self.fixture_enabled:
            row = self._cli_result(resource, question)
            chunks = row.get("chunks", [])
            hits = [
                {
                    "document_id": item.get("file_id") or item.get("document_id"),
                    "content": item.get("content", ""),
                    "score": item.get("score"),
                    "source": "moi_cli_native",
                }
                for item in chunks
                if isinstance(item, Mapping)
            ]
            return {
                "contract": "moi_cli_native",
                "hits": hits[: int(self.args.top_k)],
                "payload": row,
            }
        package = resource.get("canonical_package", {})
        documents = package.get("documents", []) if isinstance(package, Mapping) else []
        wanted = set(question.document_ids)
        hits = [
            {
                "document_id": item.get("id"),
                "content": item.get("content", ""),
                "score": 1.0,
                "source": "moi_canonical_package_fixture",
            }
            for item in documents
            if isinstance(item, Mapping) and (not wanted or str(item.get("id")) in wanted)
        ]
        return {
            "contract": "moi_canonical_package_fixture",
            "hits": hits[: int(self.args.top_k)],
            "payload": {"question": self.canonical_question(question, source_dataset=self.package.dataset)},
        }

    def qa(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        self._supported("native_qa", question.media)
        if not self.fixture_enabled:
            row = self._cli_result(resource, question)
            return {
                "contract": "moi_cli_native",
                "answer": str(row.get("answer", "")),
                "generation_provider": str(row.get("generation_provider") or self.context.profiles["llm"].name),
                "generation_model": str(row.get("generation_model") or self.context.profiles["llm"].model),
                "thinking": dict(THINKING_CONFIG),
                "payload": row,
            }
        return {
            "contract": "moi_canonical_package_fixture",
            "answer": question.answer,
            "generation_provider": self.context.profiles["llm"].name,
            "generation_model": self.context.profiles["llm"].model,
            "thinking": dict(THINKING_CONFIG),
        }


class _StartRateLimiter:
    """Serialize request starts to respect a provider's RPS quota.

    The lock is held only while reserving the next start time. Network I/O
    remains concurrent, so a slow completion does not serialize all workers.
    """

    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, float(interval_seconds))
        self._lock = threading.Lock()
        self._next_start = 0.0

    def wait_for_slot(self) -> None:
        if self.interval_seconds <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_start - now)
            self._next_start = max(now, self._next_start) + self.interval_seconds
        if delay:
            time.sleep(delay)


class DifyAdapter(BaseAdapter):
    def __init__(self, context: RunnerContext):
        super().__init__(context)
        self.max_indexing_scopes = _dify_max_indexing_scopes(self.args, self.env)
        self.dataset_key = value_from(self.env, "DIFY_LOCAL_DATASET_API_KEY", "DIFY_DATASET_API_KEY")
        self.configured_dataset_id = value_from(self.env, "DIFY_LOCAL_DATASET_ID", "DIFY_DATASET_ID")
        selected_embedding_provider = value_from(self.env, "DIFY_MAAS_EMBEDDING_PROVIDER")
        legacy_embedding_provider = value_from(self.env, "DIFY_EMBEDDING_PROVIDER")
        if legacy_embedding_provider and legacy_embedding_provider != selected_embedding_provider:
            raise RunnerError("DIFY_MAAS_EMBEDDING_PROVIDER_REQUIRED")
        configured_adapters = [selected_embedding_provider]
        llm_provider_key = "DIFY_DEEPSEEK_LLM_PROVIDER"
        configured_adapters.append(value_from(self.env, llm_provider_key))
        # Provider IDs are discovered from Dify's active model registry. A
        # legacy generic/Qianfan selector is never a compatibility fallback.
        _reject_dify_adapters(
            configured_adapters,
            allow_generic_compat=False,
        )

    def _embedding_provider(self) -> str:
        return value_from(
            self.env,
            "DIFY_MAAS_EMBEDDING_PROVIDER",
            default="NOT_CONFIGURED",
        )

    def _create_dataset(self, key: str, documents: list[Document]) -> str:
        if self._configured_resource_allowed(key, self.configured_dataset_id):
            return self.configured_dataset_id
        if not self.dataset_key:
            raise ProviderUnavailable("DIFY_LOCAL_DATASET_API_KEY_MISSING")
        payload = self.client.request(
            "POST",
            "/datasets",
            api_key=self.dataset_key,
            json_body={
                "name": _bounded_unique_name(f"CompetitorEval-{self.args.run_id}-{key}", 40),
                "indexing_technique": "high_quality",
                "permission": "only_me",
                "embedding_model": self.context.profiles["maas"].embedding_model,
                "embedding_model_provider": self._embedding_provider(),
            },
            operation=f"dify-create-dataset-{_safe_name(key)}",
            timeout=self.args.upload_timeout,
        )
        dataset_id = self._id(payload, ("id", "dataset_id"))
        if not dataset_id:
            raise EvalError("DIFY_CREATE_DATASET_NO_ID")
        return dataset_id

    def _console_request(self, binding: Any, method: str, path: str, body: Any, operation: str) -> Any:
        artifact = self.context.root / "http" / f"dify-console-{_safe_name(operation)}.json"
        try:
            response = binding._request(method, path, body)
        except Exception as exc:
            json_dump(artifact, {"operation": operation, "request": {"method": method, "path": path, "body": body}, "error": _error_text(exc)})
            raise
        json_dump(artifact, {"operation": operation, "request": {"method": method, "path": path, "body": body}, "response": response})
        return response

    def _create_native_app(self, key: str, dataset_id: str) -> dict[str, Any]:
        binding = DifyAppBinding(self.env, "", self.context.progress)
        binding._login()
        created = self._console_request(
            binding,
            "POST",
            "/console/api/apps",
            {
                "name": _bounded_unique_name(f"CompetitorEval-{self.args.run_id}-{key}", 40),
                "description": "isolated competitor-eval-ready-v1 native QA",
                "mode": "chat",
                "icon_type": "emoji",
                "icon": "🤖",
                "icon_background": "#FFEAD5",
            },
            f"create-app-{key}",
        )
        app_id = self._id(created, ("id", "app_id"))
        if not app_id:
            raise ContractUnsupported("DIFY_CONSOLE_CREATE_APP_NO_ID")
        binding.app_id = app_id
        binding.bind(dataset_id, self.context.root / "native-bindings" / _safe_name(key))

        detail = self._console_request(binding, "GET", f"/console/api/apps/{app_id}", None, f"get-bound-app-{key}")
        detail = binding._unwrap(detail)
        model_config = detail.get("model_config")
        if not isinstance(model_config, dict):
            raise ContractUnsupported("DIFY_CONSOLE_NEW_APP_MODEL_CONFIG_MISSING")
        model_config = json.loads(json.dumps(model_config))
        model = model_config.get("model")
        if not isinstance(model, dict):
            model = {}
        else:
            model = json.loads(json.dumps(model))
        selected_llm = self.context.profiles["llm"]
        model["name"] = selected_llm.model
        model["mode"] = "chat"
        provider_key = "DIFY_DEEPSEEK_LLM_PROVIDER"
        provider = value_from(self.env, provider_key)
        if not provider:
            raise ProviderUnavailable("DIFY_DEEPSEEK_LLM_PROVIDER_MISSING")
        if not provider:
            provider = selected_llm.name
        model["provider"] = provider
        completion_params = model.get("completion_params")
        if not isinstance(completion_params, dict):
            completion_params = {}
        # The installed Dify DeepSeek plugin validates this parameter as a
        # boolean; the runner-level disabled-thinking contract is represented
        # as False at Dify's native API boundary.
        completion_params["thinking"] = False
        completion_params["max_tokens"] = DEFAULT_MAX_OUTPUT_TOKENS
        model["completion_params"] = completion_params
        model_config["model"] = model
        model_config["pre_prompt"] = SHARED_PROMPT_TEXT
        self._console_request(
            binding,
            "POST",
            f"/console/api/apps/{app_id}/model-config",
            model_config,
            f"configure-bound-app-{key}",
        )
        key_payload = self._console_request(
            binding,
            "POST",
            f"/console/api/apps/{app_id}/api-keys",
            None,
            f"create-app-key-{key}",
        )
        app_key = str(first_value(key_payload, ("token", "api_key", "key")) or "")
        if not app_key:
            raise ContractUnsupported("DIFY_CONSOLE_CREATE_APP_KEY_EMPTY")
        secret_path = self.context.write_secret(key, app_key)
        return {
            "status": "ready",
            "contract": "dify_console_isolated_chat_app",
            "app_id": app_id,
            "app_key_secret_path": secret_path,
            "bound_resource_id": dataset_id,
            "model": selected_llm.model,
            "provider": selected_llm.name,
            "thinking": dict(THINKING_CONFIG),
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "prompt_contract": {
                "version": SHARED_PROMPT_VERSION,
                "hash": SHARED_PROMPT_HASH,
                "status": "propagated:dify_native_pre_prompt",
            },
        }

    def _ensure_native_app(self, key: str, resource: dict[str, Any]) -> None:
        if self._native_app_resumable(resource):
            return
        previous_native = resource.get("native_app")
        previous_app_id = resource.get("app_id")
        try:
            native = self._create_native_app(key, str(resource["dataset_id"]))
            if isinstance(previous_native, Mapping) and previous_app_id:
                native["replaced_native_app"] = {
                    "app_id": str(previous_app_id),
                    "model": previous_native.get("model"),
                    "provider": previous_native.get("provider"),
                    "reason": "resume_provider_adjustment",
                }
            resource.update({"app_id": native["app_id"], "app_key_secret_path": native["app_key_secret_path"], "native_app": native})
        except Exception as exc:
            self._record_native_setup_failure(resource, exc)

    def _upload_document(self, dataset_id: str, document: Document, ordinal: int) -> str:
        if not self.dataset_key:
            raise ProviderUnavailable("DIFY_LOCAL_DATASET_API_KEY_MISSING")
        if document.artifact_path is None:
            raise EvalError(f"DIFY_DOCUMENT_ARTIFACT_MISSING:{document.document_id}")
        response = self.client.request(
            "POST",
            f"/datasets/{dataset_id}/document/create-by-file",
            api_key=self.dataset_key,
            file_path=document.artifact_path,
            form={
                "data": json.dumps(
                    {
                        "indexing_technique": "high_quality",
                        "process_rule": {
                            "mode": "custom",
                            "rules": {
                                "pre_processing_rules": [{"id": "remove_extra_spaces", "enabled": True}, {"id": "remove_urls_emails", "enabled": False}],
                                "segmentation": {"separator": "\n", "max_tokens": 512, "chunk_overlap": 64},
                            },
                        },
                    },
                    ensure_ascii=False,
                )
            },
            operation=f"dify-upload-{ordinal:04d}-{_safe_name(document.document_id)}",
            timeout=self.args.upload_timeout,
        )
        document_id = self._id(response, ("document", "document_id", "id"))
        if not document_id:
            raise EvalError(f"DIFY_UPLOAD_NO_ID:{document.document_id}")
        return document_id

    def _persist_scope_state(self, key: str, resource: dict[str, Any], phase: str, **fields: Any) -> None:
        """Checkpoint one document-local scope after every external transition.

        The runner deliberately keeps one ArtifactHTTP client on one thread.
        ``resource-map.json`` is therefore the durable state machine boundary:
        an interruption can reconstruct the pending window from resource
        status and upload checkpoints without sharing a client across workers.
        """

        persist = bool(fields.pop("_persist", True))
        state = dict(resource.get("ingest_state") or {})
        state.update({"phase": phase, "updated_at": utc_now()})
        state["active_indexing"] = phase in {"indexing", "polling"}
        state["checkpoint_count"] = int(state.get("checkpoint_count", 0) or 0) + 1
        for name, value in fields.items():
            if value is not None:
                state[name] = value
        if phase not in {"failed", "blocked"}:
            state.pop("error", None)
        resource["ingest_state"] = state
        if not persist:
            return
        self._save_resource(key, resource)
        self.context.progress_emit(
            "ingest",
            f"dify scope {key}: {phase}",
            resource_key=key,
            phase=phase,
            active_indexing=state["active_indexing"],
            checkpoint_count=state["checkpoint_count"],
        )

    def _scope_resource(self, key: str, candidates: list[Document]) -> dict[str, Any]:
        return self._existing(key) or {
            "resource_key": key,
            "document_ids": [document.document_id for document in candidates],
            "documents": {},
        }

    def _prepare_document_local_scope(self, key: str, candidates: list[Document]) -> dict[str, Any]:
        resource = self._scope_resource(key, candidates)
        artifacts, unsupported = self._ingest_selection(key, candidates, resource)
        resource.pop("error", None)
        if not artifacts:
            resource.update({"status": "unsupported", "error": "NO_SUPPORTED_DOCUMENTS", "resource_id": None, "ready": False})
            self._persist_scope_state(
                key,
                resource,
                "unsupported",
                expected_documents=0,
                submitted_documents=0,
                unsupported_document_count=len(unsupported),
            )
            return {"terminal_status": "UNSUPPORTED", "resource": resource}

        current_status = str(resource.get("status", "")).casefold()
        if current_status in {"ready", "partial"} and resource.get("resource_id"):
            # A completed scope is already durable.  Only repair a missing
            # native binding; never re-upload it during a resumed run.
            self._ensure_native_app(key, resource)
            self._save_resource(key, resource)
            return {
                "terminal_status": "UNSUPPORTED" if current_status == "partial" else "READY",
                "resource": resource,
            }

        resource.update({"status": "not_started" if not resource.get("resource_id") else current_status or "resuming", "ready": False})
        self._persist_scope_state(
            key,
            resource,
            "planned",
            expected_documents=len(artifacts),
            submitted_documents=0,
            unsupported_document_count=len(unsupported),
        )
        return {
            "key": key,
            "candidates": candidates,
            "artifacts": artifacts,
            "unsupported": unsupported,
            "resource": resource,
        }

    def _start_document_local_scope(self, work: dict[str, Any]) -> dict[str, Any]:
        key = str(work["key"])
        resource = work["resource"]
        artifacts: list[Document] = work["artifacts"]
        resource.setdefault("uploads", {})
        resource.pop("error", None)
        dataset_id = str(resource.get("resource_id") or resource.get("dataset_id") or "")
        if not dataset_id:
            resource["status"] = "starting"
            self._persist_scope_state(key, resource, "starting", expected_documents=len(artifacts))
            dataset_id = str(self._create_dataset(key, artifacts))
            resource.update(
                {
                    "resource_id": dataset_id,
                    "dataset_id": dataset_id,
                    "resource_origin": (
                        "configured_opt_in"
                        if self._configured_resource_allowed(key, self.configured_dataset_id)
                        and dataset_id == self.configured_dataset_id
                        else "created_for_run"
                    ),
                }
            )
            self._persist_scope_state(key, resource, "dataset_created", expected_documents=len(artifacts))
        else:
            resource.update({"resource_id": dataset_id, "dataset_id": dataset_id})
            self._persist_scope_state(
                key,
                resource,
                "resuming",
                expected_documents=len(artifacts),
                submitted_documents=sum(
                    1
                    for document in artifacts
                    if isinstance(resource["uploads"].get(document.document_id), Mapping)
                    and resource["uploads"].get(document.document_id, {}).get("remote_id")
                ),
            )

        configured_reuse = (
            resource.get("resource_origin") == "configured_opt_in"
            and self._configured_resource_allowed(key, self.configured_dataset_id)
            and dataset_id == self.configured_dataset_id
        )
        uploaded = 0
        for ordinal, document in enumerate(artifacts, start=1):
            doc_state = resource["uploads"].get(document.document_id, {})
            has_remote_id = bool(isinstance(doc_state, Mapping) and doc_state.get("remote_id"))
            if configured_reuse:
                resource["uploads"][document.document_id] = {
                    **(dict(doc_state) if isinstance(doc_state, Mapping) else {}),
                    "status": "submitted",
                    "sha256": document.source_hash,
                    "origin": "configured_resource",
                }
                uploaded += 1
                continue
            if has_remote_id and str(doc_state.get("status")) in {"submitted", "ready"}:
                uploaded += 1
                continue
            resource["status"] = "uploading"
            self._persist_scope_state(
                key,
                resource,
                "uploading",
                current_document_id=document.document_id,
                expected_documents=len(artifacts),
                submitted_documents=uploaded,
            )
            remote_id = self._upload_document(dataset_id, document, ordinal)
            resource["uploads"][document.document_id] = {
                "status": "submitted",
                "remote_id": remote_id,
                "sha256": document.source_hash,
            }
            uploaded += 1
            self._persist_scope_state(
                key,
                resource,
                "uploading",
                current_document_id=document.document_id,
                expected_documents=len(artifacts),
                submitted_documents=uploaded,
            )

        if uploaded != len(artifacts):
            raise EvalError(f"DIFY_SCOPE_UPLOAD_INCOMPLETE:{key}:{uploaded}/{len(artifacts)}")
        resource["status"] = "indexing"
        resource["ready"] = False
        self._persist_scope_state(
            key,
            resource,
            "indexing",
            dataset_id=dataset_id,
            expected_documents=len(artifacts),
            submitted_documents=uploaded,
            ready_documents=0,
            poll_count=0,
        )
        return {
            **work,
            "dataset_id": dataset_id,
            "expected": uploaded,
            "deadline": time.monotonic() + float(self.args.index_timeout),
            "last_snapshot": {},
        }

    def _wait_ready_once(self, dataset_id: str, expected: int) -> dict[str, Any]:
        items: list[Any] = []
        page = 1
        while True:
            payload = self.client.request(
                "GET",
                f"/datasets/{dataset_id}/documents",
                api_key=self.dataset_key,
                params={"page": page, "limit": 100},
                operation=f"dify-readiness-{dataset_id}",
                timeout=self.args.query_timeout,
            )
            batch = list_items(payload, ("data", "documents"))
            items.extend(batch)
            raw_has_more = payload.get("has_more") if isinstance(payload, Mapping) else False
            has_more = (
                raw_has_more.strip().casefold() in {"1", "true", "yes", "on"}
                if isinstance(raw_has_more, str)
                else bool(raw_has_more)
            )
            if not has_more or not batch:
                break
            page += 1
        statuses = [str(first_value(item, ("indexing_status", "status", "state")) or "").lower() for item in items]
        snapshot: dict[str, Any] = {
            "items": len(items),
            "expected": expected,
            "statuses": dict(Counter(statuses)),
        }
        if any(status in {"error", "failed"} for status in statuses):
            raise EvalError(f"DIFY_INDEX_FAILED:{snapshot}")
        snapshot["ready"] = bool(
            len(items) >= expected
            and expected > 0
            and all(status in {"completed", "indexed", "ready", "available"} for status in statuses)
        )
        return snapshot

    def _finish_document_local_scope(self, work: dict[str, Any], index: dict[str, Any]) -> None:
        key = str(work["key"])
        resource = work["resource"]
        artifacts: list[Document] = work["artifacts"]
        candidates: list[Document] = work["candidates"]
        unsupported: dict[str, str] = work["unsupported"]
        for document in artifacts:
            resource["uploads"].setdefault(document.document_id, {}).update(
                {"status": "ready", "sha256": document.source_hash}
            )
        self._mark_candidates_ready(resource, candidates, unsupported)
        resource.update(
            {
                "status": "partial" if unsupported else "ready",
                "ready": True,
                "index": index,
            }
        )
        resource.pop("error", None)
        self._persist_scope_state(
            key,
            resource,
            "binding_app",
            dataset_id=work["dataset_id"],
            expected_documents=work["expected"],
            submitted_documents=work["expected"],
            ready_documents=work["expected"],
            poll_count=int(resource.get("ingest_state", {}).get("poll_count", 0) or 0),
            last_snapshot=index,
        )
        # Binding is deliberately after the service has reported the whole
        # scope searchable.  _ensure_native_app records a blocked/unsupported
        # binding without losing the ready resource checkpoint.
        self._ensure_native_app(key, resource)
        self._persist_scope_state(
            key,
            resource,
            "ready",
            dataset_id=work["dataset_id"],
            expected_documents=work["expected"],
            submitted_documents=work["expected"],
            ready_documents=work["expected"],
            last_snapshot=index,
            native_app_status=(resource.get("native_app") or {}).get("status") if isinstance(resource.get("native_app"), Mapping) else None,
        )

    def _fail_document_local_scope(self, key: str, resource: dict[str, Any], exc: BaseException) -> str:
        status = _exception_status(exc)
        resource.update(
            {
                "status": "blocked" if status == "BLOCKED" else "failed",
                "ready": False,
                "error": _error_text(exc),
            }
        )
        self._persist_scope_state(
            key,
            resource,
            "blocked" if status == "BLOCKED" else "failed",
            error=_error_text(exc),
            failure_status=status,
            active_indexing=False,
        )
        return status

    def _ingest_document_local(self) -> dict[str, Any]:
        counts: Counter[str] = Counter()
        pending: list[dict[str, Any]] = []
        for key, candidates in self._target_resources():
            resource = self._scope_resource(key, candidates)
            try:
                work = self._prepare_document_local_scope(key, candidates)
                terminal_status = work.get("terminal_status")
                if terminal_status:
                    counts[terminal_status] += 1
                else:
                    pending.append(work)
            except Exception as exc:
                counts[self._fail_document_local_scope(key, resource, exc)] += 1

        active: dict[str, dict[str, Any]] = {}
        max_scopes = self.max_indexing_scopes
        while pending or active:
            while pending and len(active) < max_scopes:
                work = pending.pop(0)
                key = str(work["key"])
                try:
                    active[key] = self._start_document_local_scope(work)
                except Exception as exc:
                    counts[self._fail_document_local_scope(key, work["resource"], exc)] += 1

            if not active:
                continue

            made_progress = False
            for key, work in list(active.items()):
                try:
                    if time.monotonic() >= float(work["deadline"]):
                        raise EvalError(f"DIFY_INDEX_TIMEOUT:{work.get('last_snapshot', {})}")
                    snapshot = self._wait_ready_once(str(work["dataset_id"]), int(work["expected"]))
                    work["last_snapshot"] = snapshot
                    state = dict(work["resource"].get("ingest_state") or {})
                    poll_count = int(state.get("poll_count", 0) or 0) + 1
                    if snapshot.get("ready"):
                        self._finish_document_local_scope(work, snapshot)
                        counts["UNSUPPORTED" if work["unsupported"] else "READY"] += 1
                        active.pop(key, None)
                        made_progress = True
                    else:
                        work["resource"]["status"] = "indexing"
                        self._persist_scope_state(
                            key,
                            work["resource"],
                            "polling",
                            # The resource map can be hundreds of KiB for
                            # document-local benchmarks. Persist the first
                            # poll and then every ~30 seconds (at the default
                            # 2-second interval) instead of rewriting it for
                            # every readiness request.
                            _persist=poll_count == 1 or poll_count % 15 == 0,
                            dataset_id=work["dataset_id"],
                            expected_documents=work["expected"],
                            submitted_documents=work["expected"],
                            ready_documents=snapshot.get("items", 0),
                            poll_count=poll_count,
                            last_snapshot=snapshot,
                        )
                except Exception as exc:
                    counts[self._fail_document_local_scope(key, work["resource"], exc)] += 1
                    active.pop(key, None)
                    made_progress = True
            if active and not made_progress:
                time.sleep(float(self.args.poll_seconds))

        status = "SUCCESS" if not any(counts.get(value) for value in ("FAILED", "BLOCKED", "TIMEOUT")) else "PARTIAL"
        return {"status": status, "resource_counts": dict(counts), "resources": self._resources_payload().get("resources", {})}

    def _wait_ready(self, dataset_id: str, expected: int) -> dict[str, Any]:
        deadline = time.monotonic() + float(self.args.index_timeout)
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self._wait_ready_once(dataset_id, expected)
            if last.get("ready"):
                return last
            time.sleep(float(self.args.poll_seconds))
        raise EvalError(f"DIFY_INDEX_TIMEOUT:{last}")

    def ingest(self) -> dict[str, Any]:
        if self.package.scope == "document_local":
            return self._ingest_document_local()
        counts: Counter[str] = Counter()
        for key, candidates in self._target_resources():
            existing = self._existing(key) or {"resource_key": key, "document_ids": [document.document_id for document in candidates], "documents": {}}
            artifacts, unsupported = self._ingest_selection(key, candidates, existing)
            if not artifacts:
                existing.update({"status": "unsupported", "error": "NO_SUPPORTED_DOCUMENTS", "resource_id": None})
                self._save_resource(key, existing)
                counts["UNSUPPORTED"] += 1
                continue
            try:
                dataset_id = str(existing.get("resource_id") or self._create_dataset(key, artifacts))
                existing["resource_id"] = dataset_id
                existing["dataset_id"] = dataset_id
                existing.setdefault(
                    "resource_origin",
                    "configured_opt_in"
                    if self._configured_resource_allowed(key, self.configured_dataset_id) and dataset_id == self.configured_dataset_id
                    else "created_for_run",
                )
                existing.setdefault("uploads", {})
                configured_reuse = (
                    existing.get("resource_origin") == "configured_opt_in"
                    and self._configured_resource_allowed(key, self.configured_dataset_id)
                    and dataset_id == self.configured_dataset_id
                )
                uploaded = len(artifacts) if configured_reuse else 0
                if configured_reuse:
                    existing.update(
                        {
                            "status": "verifying_configured_resource",
                            "uploaded": 0,
                            "reused_without_upload": True,
                        }
                    )
                    self._save_resource(key, existing)
                else:
                    for ordinal, document in enumerate(artifacts, start=1):
                        doc_state = existing["uploads"].get(document.document_id, {})
                        if doc_state.get("status") in {"submitted", "ready"} and doc_state.get("remote_id"):
                            uploaded += 1
                            continue
                        remote_id = self._upload_document(dataset_id, document, ordinal)
                        existing["uploads"][document.document_id] = {"status": "submitted", "remote_id": remote_id, "sha256": document.source_hash}
                        uploaded += 1
                        existing.update({"status": "uploading", "uploaded": uploaded})
                        self._save_resource(key, existing)
                index = self._wait_ready(dataset_id, uploaded)
                for document in artifacts:
                    existing["uploads"].setdefault(document.document_id, {}).update(
                        {
                            "status": "ready",
                            "sha256": document.source_hash,
                            **({"origin": "configured_resource"} if configured_reuse else {}),
                        }
                    )
                self._mark_candidates_ready(existing, candidates, unsupported)
                existing.update({"status": "partial" if unsupported else "ready", "ready": True, "index": index})
                self._ensure_native_app(key, existing)
                self._save_resource(key, existing)
                counts["UNSUPPORTED" if unsupported else "READY"] += 1
            except Exception as exc:
                existing.update({"status": "blocked" if _exception_status(exc) == "BLOCKED" else "failed", "error": _error_text(exc), "resource_id": existing.get("resource_id")})
                self._save_resource(key, existing)
                counts[_exception_status(exc)] += 1
        status = "SUCCESS" if counts.get("FAILED", 0) == 0 and counts.get("BLOCKED", 0) == 0 else "PARTIAL"
        return {"status": status, "resource_counts": dict(counts), "resources": self._resources_payload().get("resources", {})}

    def retrieve(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        self._supported("retrieval", question.media)
        dataset_id = str(resource.get("dataset_id") or resource.get("resource_id") or "")
        if not dataset_id:
            raise RunnerError("DIFY_DATASET_ID_MISSING")
        if not self.dataset_key:
            raise ProviderUnavailable("DIFY_LOCAL_DATASET_API_KEY_MISSING")
        query = question.text[:DIFY_RETRIEVAL_QUERY_MAX_CHARS]
        payload = self.client.request(
            "POST",
            f"/datasets/{dataset_id}/retrieve",
            api_key=self.dataset_key,
            json_body={"query": query, "retrieval_model": {"search_method": "semantic_search", "reranking_enable": False, "top_k": int(self.args.top_k), "score_threshold_enabled": False}},
            operation=f"dify-retrieval-{_safe_name(question.question_id)}",
            timeout=self.args.query_timeout,
        )
        return {
            "contract": "public_direct_retrieval",
            "payload": payload,
            "hits": list_items(payload, ("records", "retriever_resources")),
            "retrieval_query_chars": len(query),
            "retrieval_query_truncated": len(query) < len(question.text),
        }

    def qa(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        self._supported("native_qa", question.media)
        if question.image_paths:
            raise ContractUnsupported("IMAGE_INPUT_UNSUPPORTED_BY_DIFY_CHAT_MESSAGES_CONTRACT")
        native = resource.get("native_app")
        if not isinstance(native, Mapping) or native.get("status") != "ready":
            reason = native.get("error") if isinstance(native, Mapping) else "DIFY_RESOURCE_NATIVE_APP_MISSING"
            if isinstance(native, Mapping) and native.get("status") == "unsupported":
                raise ContractUnsupported(str(reason))
            raise ProviderUnavailable(str(reason))
        native_key = self.context.read_secret(resource.get("app_key_secret_path"))
        payload = self.client.request(
            "POST",
            "/chat-messages",
            api_key=native_key,
            json_body={"inputs": {}, "query": question.text, "response_mode": "blocking", "conversation_id": "", "user": f"competitor-eval-{self.args.run_id}-{_safe_name(question.question_id)}", "thinking": False},
            operation=f"dify-qa-{_safe_name(question.question_id)}",
            timeout=self.args.qa_timeout,
        )
        return {
            "contract": "native_chat",
            "payload": payload,
            "answer": answer_from(payload),
            "generation_provider": self.context.profiles["llm"].name,
            "generation_model": self.context.profiles["llm"].model,
            "thinking": dict(THINKING_CONFIG),
        }


class FastGPTAdapter(BaseAdapter):
    def __init__(self, context: RunnerContext):
        super().__init__(context)
        self.api_key = value_from(self.env, "FASTGPT_API_KEY")
        self.configured_dataset_id = value_from(self.env, "FASTGPT_DATASET_ID")
        self._maas_qa_rate_limiter = self.context._maas_qa_rate_limiter

    def _create_dataset(self, key: str) -> str:
        if self._configured_resource_allowed(key, self.configured_dataset_id):
            return self.configured_dataset_id
        if not self.api_key:
            raise ProviderUnavailable("FASTGPT_API_KEY_MISSING")
        payload = self._unwrap_fastgpt(self.client.request(
            "POST",
            "/api/core/dataset/create",
            api_key=self.api_key,
            json_body={"parentId": None, "type": "dataset", "name": _bounded_unique_name(f"CompetitorEval-{self.args.run_id}-{key}", 100), "intro": "competitor-eval-ready-v1", "avatar": "", "vectorModel": self.context.profiles["maas"].embedding_model, "agentModel": self.context.profiles["llm"].model},
            operation=f"fastgpt-create-dataset-{_safe_name(key)}",
            timeout=self.args.upload_timeout,
        ))
        dataset_id = self._id(payload, ("datasetId", "id", "_id"))
        if not dataset_id:
            raise EvalError("FASTGPT_CREATE_DATASET_NO_ID")
        return dataset_id

    def _create_native_app(self, key: str, dataset_id: str) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderUnavailable("FASTGPT_API_KEY_MISSING_FOR_APP_CREATION")
        app_payload = _build_fastgpt_isolated_app_payload(
            provider_name=self.context.profiles["llm"].name,
            dataset_id=dataset_id,
            dataset_name=_bounded_unique_name(f"CompetitorEval-{self.args.run_id}-{key}", 100),
            llm_model=self.context.profiles["llm"].model,
            embedding_model=self.context.profiles["maas"].embedding_model,
        )
        for module in app_payload.get("modules", []):
            if not isinstance(module, dict) or module.get("nodeId") != "isolatedAiChat":
                continue
            for item in module.get("inputs", []):
                if isinstance(item, dict) and item.get("key") == "systemPrompt":
                    item["value"] = SHARED_PROMPT_TEXT
        for module in app_payload.get("modules", []):
            if not isinstance(module, dict):
                continue
            for item in module.get("inputs", []):
                if not isinstance(item, dict):
                    continue
                if item.get("key") == "model":
                    item["value"] = self.context.profiles["llm"].model
                elif item.get("key") == "datasets" and isinstance(item.get("value"), list) and item["value"]:
                    item["value"][0]["datasetId"] = dataset_id
                    item["value"][0]["vectorModel"] = {"model": self.context.profiles["maas"].embedding_model}
        app_payload.setdefault("chatConfig", {})["thinking"] = dict(THINKING_CONFIG)
        app_payload["name"] = _bounded_unique_name(f"CompetitorEval-{self.args.run_id}-{key}", 100)
        app = self._unwrap_fastgpt(
            self.client.request(
                "POST",
                "/api/core/app/create",
                api_key=self.api_key,
                json_body=app_payload,
                operation=f"fastgpt-create-app-{_safe_name(key)}",
                timeout=self.args.upload_timeout,
            )
        )
        app_id = self._id(app, ("appId", "id", "_id"))
        if not app_id:
            raise ContractUnsupported("FASTGPT_CREATE_ISOLATED_APP_NO_ID")
        # FastGPT v4.15 uses team-level OpenAPI keys. Creating another key is
        # a console-login operation (`authToken: true`) and correctly rejects
        # an existing OpenAPI bearer with HTTP 403. Reuse the configured team
        # key while keeping the newly created app and chatId isolated.
        secret_path = self.context.write_secret(key, self.api_key)
        return {
            "status": "ready",
            "contract": "fastgpt_isolated_simple_rag_app",
            "key_origin": "configured_team_openapi_key",
            "app_id": app_id,
            "app_key_secret_path": secret_path,
            "bound_resource_id": dataset_id,
            "model": self.context.profiles["llm"].model,
            "provider": self.context.profiles["llm"].name,
            "thinking": dict(THINKING_CONFIG),
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "prompt_contract": {
                "version": SHARED_PROMPT_VERSION,
                "hash": SHARED_PROMPT_HASH,
                "status": "propagated:isolated_app_system_prompt",
                "max_output_tokens_status": "unsupported:platform_native_limit_not_exposed",
            },
        }

    def _ensure_native_app(self, key: str, resource: dict[str, Any]) -> None:
        if self._native_app_resumable(resource):
            return
        previous_native = resource.get("native_app")
        previous_app_id = resource.get("app_id")
        try:
            native = self._create_native_app(key, str(resource["dataset_id"]))
            if isinstance(previous_native, Mapping) and previous_app_id:
                native["replaced_native_app"] = {
                    "app_id": str(previous_app_id),
                    "model": previous_native.get("model"),
                    "provider": previous_native.get("provider"),
                    "reason": "resume_provider_adjustment",
                }
            resource.update({"app_id": native["app_id"], "app_key_secret_path": native["app_key_secret_path"], "native_app": native})
        except Exception as exc:
            self._record_native_setup_failure(resource, exc)

    def _create_collection(self, dataset_id: str, document: Document, ordinal: int) -> str:
        payload = self._unwrap_fastgpt(self.client.request(
            "POST", "/api/core/dataset/collection/create", api_key=self.api_key,
            json_body={"datasetId": dataset_id, "parentId": None, "name": document.artifact_path.name if document.artifact_path else document.document_id, "type": "virtual"},
            operation=f"fastgpt-create-collection-{ordinal:04d}", timeout=self.args.upload_timeout,
        ))
        collection_id = self._id(payload, ("collectionId", "id", "_id"))
        if not collection_id:
            raise EvalError(f"FASTGPT_CREATE_COLLECTION_NO_ID:{document.document_id}")
        return collection_id

    def _push_document(self, collection_id: str, document: Document, ordinal: int) -> None:
        if document.artifact_path is None:
            raise EvalError(f"FASTGPT_DOCUMENT_ARTIFACT_MISSING:{document.document_id}")
        content = document.artifact_path.read_text(encoding="utf-8", errors="replace")
        # MaaS bge-m3 rejects inputs above 8192 tokens.  The mixed-language
        # corpus crossed that boundary even at 16k characters; use a safer
        # ceiling while retaining the env override.
        configured_limit = value_from(self.context.env, "FASTGPT_PUSH_CHUNK_MAX_CHARS", default="8000")
        try:
            max_chars = max(1000, int(configured_limit))
        except (TypeError, ValueError):
            max_chars = 8000
        items = _fastgpt_push_items(
            content,
            document,
            max_chars,
            group_markers=str(self.package.condition).casefold() == "layout",
        )
        if not items:
            raise EvalError(f"FASTGPT_DOCUMENT_EMPTY:{document.document_id}")
        for batch_index in range(0, len(items), 200):
            batch = items[batch_index:batch_index + 200]
            self.client.request(
                "POST", "/api/core/dataset/data/pushData", api_key=self.api_key,
                json_body={"collectionId": collection_id, "trainingType": "chunk", "data": batch},
                operation=f"fastgpt-push-data-{ordinal:04d}-{batch_index // 200 + 1:03d}", timeout=self.args.upload_timeout,
            )

    def _wait_ready(self, dataset_id: str, expected: int) -> dict[str, Any]:
        timeout = float(self.args.index_timeout)
        started = time.monotonic()
        deadline = started + timeout
        # A progressing collection may legitimately need longer than one wall
        # clock timeout on a constrained local PG instance.  Progress can renew
        # the inactivity deadline, but never beyond twice the configured budget.
        hard_deadline = started + (timeout * 2)
        previous_progress: tuple[int, int, int] | None = None
        progress_extensions = 0
        last: dict[str, Any] = {}
        while time.monotonic() < min(deadline, hard_deadline):
            # FastGPT caps listV2 at 100 rows even when a larger pageSize is
            # requested.  Read every page so corpora with >100 collections do
            # not wait forever or hide failures outside the first page.
            items: list[Any] = []
            offset = 0
            total = expected
            while offset < total:
                payload = self._unwrap_fastgpt(self.client.request(
                    "POST", "/api/core/dataset/collection/listV2", api_key=self.api_key,
                    json_body={"offset": offset, "pageSize": 100, "datasetId": dataset_id, "parentId": None, "searchText": ""},
                    operation=f"fastgpt-readiness-{dataset_id}-offset-{offset:04d}", timeout=self.args.query_timeout,
                ))
                page = list_items(payload, ("list",))
                items.extend(page)
                if isinstance(payload, Mapping):
                    total = int(payload.get("total", total) or total)
                if not page:
                    break
                offset += len(page)
            failed = [item for item in items if isinstance(item, dict) and (item.get("hasError") or item.get("finalErrorAmount", 0))]
            training = sum(int(item.get("trainingAmount", 0) or 0) for item in items if isinstance(item, dict))
            active = sum(int(item.get("activeTrainingAmount", 0) or 0) for item in items if isinstance(item, dict))
            data = sum(int(item.get("dataAmount", 0) or 0) for item in items if isinstance(item, dict))
            current_progress = (training, active, data)
            now = time.monotonic()
            if previous_progress is not None and (
                training < previous_progress[0]
                or active < previous_progress[1]
                or data > previous_progress[2]
            ):
                extended_deadline = min(hard_deadline, now + timeout)
                if extended_deadline > deadline:
                    deadline = extended_deadline
                    progress_extensions += 1
            previous_progress = current_progress
            last = {
                "collections": len(items),
                "expected": expected,
                "training": training,
                "active_training": active,
                "errors": len(failed),
                "data": data,
                "progress_extensions": progress_extensions,
                "failed_collection_ids": [
                    str(item.get("_id", item.get("id", "")))
                    for item in failed
                    if isinstance(item, Mapping)
                ],
                "failed_collection_names": [
                    str(item.get("name", ""))
                    for item in failed
                    if isinstance(item, Mapping)
                ],
            }
            # FastGPT exposes final errors while sibling training jobs are still
            # active.  Wait for the collection to reach a terminal state so a
            # single failed chunk does not make the runner abandon the rest of
            # the in-flight indexing work (and so the recorded counts are final).
            if failed and active == 0:
                # Preserve a partial corpus when a provider permanently rejects
                # individual source documents (for example content moderation).
                # The failed collections remain explicit in the resource map;
                # questions are never removed from the evaluation denominator.
                return last
            if len(items) >= expected and expected > 0 and training == 0 and active == 0:
                return last
            time.sleep(float(self.args.poll_seconds))
        raise EvalError(f"FASTGPT_INDEX_TIMEOUT:{last}")

    def _ingest_resource(self, key: str, candidates: list[Document]) -> str:
        existing = self._existing(key) or {
            "resource_key": key,
            "document_ids": [document.document_id for document in candidates],
            "documents": {},
        }
        try:
            artifacts, unsupported = self._ingest_selection(key, candidates, existing)
            self._save_resource(key, existing)
            if not artifacts:
                existing.update({"status": "unsupported", "error": "NO_SUPPORTED_DOCUMENTS", "resource_id": None})
                self._save_resource(key, existing)
                return "UNSUPPORTED"

            existing.update({"status": "starting", "ready": False})
            dataset_id = str(
                existing.get("dataset_id")
                or existing.get("resource_id")
                or self._create_dataset(key)
            )
            existing.update({"dataset_id": dataset_id, "resource_id": dataset_id})
            existing.setdefault(
                "resource_origin",
                "configured_opt_in"
                if self._configured_resource_allowed(key, self.configured_dataset_id)
                and dataset_id == self.configured_dataset_id
                else "created_for_run",
            )
            if not isinstance(existing.get("uploads"), dict):
                existing["uploads"] = {}
            self._save_resource(key, existing)

            created = 0
            for ordinal, document in enumerate(artifacts, start=1):
                state = existing["uploads"].get(document.document_id, {})
                if not isinstance(state, Mapping):
                    state = {}
                if state.get("status") in {"ready", "submitted"} and state.get("collection_id"):
                    created += 1
                    continue
                collection_id = str(
                    state.get("collection_id")
                    or self._create_collection(dataset_id, document, ordinal)
                )
                existing["status"] = "indexing"
                existing["uploads"][document.document_id] = {
                    **dict(state),
                    "status": "collection_created",
                    "collection_id": collection_id,
                    "sha256": document.source_hash,
                }
                self._save_resource(key, existing)
                self._push_document(collection_id, document, ordinal)
                existing["uploads"][document.document_id]["status"] = "submitted"
                created += 1
                # A successful pushData is durable before readiness polling so
                # a failed poll resumes without creating a duplicate collection.
                self._save_resource(key, existing)

            index = self._wait_ready(dataset_id, created)
            failed_collection_ids = set(index.get("failed_collection_ids", []))
            failed_document_ids: list[str] = []
            for document in artifacts:
                upload = existing["uploads"].setdefault(document.document_id, {})
                if str(upload.get("collection_id", "")) in failed_collection_ids:
                    upload["status"] = "failed"
                    upload["error"] = "FASTGPT_INDEX_PROVIDER_REJECTED"
                    failed_document_ids.append(document.document_id)
                else:
                    upload["status"] = "ready"
            self._mark_candidates_ready(existing, candidates, unsupported)
            existing["ready_document_count"] = max(
                0,
                int(existing.get("ready_document_count", 0)) - len(failed_document_ids),
            )
            existing["failed_document_count"] = len(failed_document_ids)
            existing["failed_document_ids_preview"] = failed_document_ids[:10]
            existing.update(
                {
                    "status": "partial" if unsupported or failed_document_ids else "ready",
                    "ready": True,
                    "index": index,
                }
            )
            self._ensure_native_app(key, existing)
            self._save_resource(key, existing)
            return "UNSUPPORTED" if unsupported else "READY"
        except Exception as exc:
            status = _exception_status(exc)
            existing.update(
                {
                    "status": "blocked" if status == "BLOCKED" else "failed",
                    "ready": False,
                    "error": _error_text(exc),
                }
            )
            self._save_resource(key, existing)
            return status

    def ingest(self) -> dict[str, Any]:
        targets = list(self._target_resources())
        statuses: list[str] = []
        if targets:
            with ThreadPoolExecutor(
                max_workers=min(int(self.args.fastgpt_ingest_concurrency), len(targets)),
                thread_name_prefix="fastgpt-ingest",
            ) as executor:
                futures = [
                    executor.submit(self._ingest_resource, key, candidates)
                    for key, candidates in targets
                ]
                # Consume futures in package order. Workers may finish in any
                # order, but counts and the returned resource map stay stable.
                statuses = [future.result() for future in futures]

        counts: Counter[str] = Counter()
        for status in statuses:
            counts[status] += 1
        payload = self._resources_payload()
        resources = payload.get("resources", {})
        if isinstance(resources, dict):
            ordered_resources = {
                key: resources[key]
                for key, _ in targets
                if key in resources
            }
            ordered_resources.update(
                {key: resources[key] for key in sorted(resources) if key not in ordered_resources}
            )
            payload["resources"] = ordered_resources
            self.context.save_resources(payload)
        status = "SUCCESS" if not any(
            counts.get(value) for value in ("FAILED", "BLOCKED", "TIMEOUT", "INTERRUPTED")
        ) else "PARTIAL"
        return {"status": status, "resource_counts": dict(counts), "resources": payload.get("resources", {})}

    def retrieve(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        self._supported("retrieval", question.media)
        if not self.api_key:
            raise ProviderUnavailable("FASTGPT_API_KEY_MISSING")
        payload = self._unwrap_fastgpt(self.client.request(
            "POST", "/api/core/dataset/searchTest", api_key=self.api_key,
            # FastGPT's `limit` is a returned-token budget, not the number of
            # records.  Passing top_k here truncates almost every response to
            # one chunk.  Request the API's documented maximum token budget,
            # then enforce top_k on the decoded list below.
            json_body={"datasetId": resource.get("dataset_id", resource.get("resource_id")), "text": question.text, "limit": 20000, "similarity": 0, "searchMode": "embedding", "usingReRank": False, "datasetSearchUsingExtensionQuery": False},
            operation=f"fastgpt-retrieval-{_safe_name(question.question_id)}", timeout=self.args.query_timeout,
        ))
        hits = list_items(payload, ("list",))
        if str(self.package.dataset).strip().casefold().replace("_", "-") == "multihop-rag":
            unique_hits: list[Any] = []
            seen_sources: set[str] = set()
            for hit in hits:
                source = str(hit.get("sourceName", "")) if isinstance(hit, Mapping) else ""
                key = source or str(hit.get("collectionId", hit.get("id", ""))) if isinstance(hit, Mapping) else ""
                if key in seen_sources:
                    continue
                seen_sources.add(key)
                unique_hits.append(hit)
                if len(unique_hits) >= int(self.args.top_k):
                    break
            hits = unique_hits
        else:
            hits = hits[: int(self.args.top_k)]
        return {"contract": "public_direct_retrieval", "payload": payload, "hits": hits}

    def qa(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        self._supported("native_qa", question.media)
        native = resource.get("native_app")
        if not isinstance(native, Mapping) or native.get("status") != "ready":
            reason = native.get("error") if isinstance(native, Mapping) else "FASTGPT_RESOURCE_NATIVE_APP_MISSING"
            if isinstance(native, Mapping) and native.get("status") == "unsupported":
                raise ContractUnsupported(str(reason))
            raise ProviderUnavailable(str(reason))
        app_id = str(resource.get("app_id") or "")
        if not app_id:
            raise ProviderUnavailable("FASTGPT_RESOURCE_APP_ID_MISSING")
        app_key = self.context.read_secret(resource.get("app_key_secret_path"))
        self._maas_qa_rate_limiter.wait_for_slot()
        payload = self._unwrap_fastgpt(self.client.request(
            "POST", "/api/v1/chat/completions", api_key=app_key,
            json_body={"appId": app_id, "chatId": str(uuid.uuid4()), "stream": False, "detail": True, "thinking": dict(THINKING_CONFIG), "messages": [{"role": "user", "content": self._question_message(question)}]},
            operation=f"fastgpt-qa-{_safe_name(question.question_id)}", timeout=self.args.qa_timeout,
        ))
        return {
            "contract": "native_chat",
            "payload": payload,
            "answer": answer_from(payload),
            "generation_provider": self.context.profiles["llm"].name,
            "generation_model": self.context.profiles["llm"].model,
            "thinking": dict(THINKING_CONFIG),
        }


class MaxKBAdapter(BaseAdapter):
    def __init__(self, context: RunnerContext):
        super().__init__(context)
        self.admin_key = value_from(self.env, "MAXKB_ADMIN_TOKEN")
        if not self.admin_key:
            token_path = ROOT / ".local-services/maxkb_local/secrets/admin.token"
            if token_path.exists():
                self.admin_key = token_path.read_text(encoding="utf-8", errors="replace").strip()
        self.admin_base = value_from(self.env, "MAXKB_ADMIN_BASE_URL", default="http://127.0.0.1:8090/admin/api")
        self.client = context._http(self.admin_base)
        self.configured_knowledge_id = value_from(self.env, "MAXKB_KNOWLEDGE_ID")
        # MaaS model IDs may be explicit because MaxKB permits duplicate
        # OpenAI records with the same provider/type/name. Never silently pick
        # an old TaaS/Qianfan vector record when an explicit ID is available.
        self.embedding_model_id = value_from(self.env, "MAXKB_EMBEDDING_MODEL_ID")
        self.chat_model_id = value_from(self.env, "MAXKB_LLM_MODEL_ID") or value_from(
            self.env, "MAXKB_CHAT_MODEL_ID"
        )
        self._qa_rate_limiter = self.context._maas_qa_rate_limiter
        self.public_base = self._public_base_url()
        _reject_taas([self.admin_base, self.public_base])

    def _public_base_url(self) -> str:
        explicit = value_from(self.env, "MAXKB_PUBLIC_BASE_URL")
        if explicit:
            return explicit.rstrip("/")
        historical = value_from(self.env, "MAXKB_OPENAI_BASE_URL")
        if historical:
            parts = urlsplit(historical.rstrip("/"))
            marker = "/chat/api"
            if marker in parts.path:
                prefix = parts.path.split(marker, 1)[0] + marker
                return urlunsplit((parts.scheme, parts.netloc, prefix, "", "")).rstrip("/")
        return str(CONTRACTS["systems"]["maxkb_local"]["public_base_url_default"]).rstrip("/")

    def _admin(self, method: str, path: str, *, body: Any = None, operation: str, timeout: float | None = None) -> Any:
        if not self.admin_key:
            raise ProviderUnavailable("MAXKB_ADMIN_TOKEN_MISSING")
        for attempt in range(MAXKB_INGEST_RETRY_LIMIT + 1):
            try:
                payload = self.client.request(
                    method,
                    path,
                    api_key=self.admin_key,
                    json_body=body,
                    operation=operation,
                    timeout=timeout or self.args.query_timeout,
                )
                if _maxkb_retryable_payload(payload):
                    if attempt < MAXKB_INGEST_RETRY_LIMIT:
                        time.sleep(_maxkb_retry_delay(attempt))
                        continue
                if isinstance(payload, dict) and int(payload.get("code", 200) or 200) not in (200, 0):
                    raise EvalError(f"MAXKB_API_ERROR:{payload.get('code')}:{payload.get('message', '')}")
                return payload.get("data", payload) if isinstance(payload, dict) and "data" in payload else payload
            except Exception as exc:
                # MaxKB may briefly reject a cached admin token while its
                # auth/session state is refreshed. Re-read the local token and
                # retry a bounded number of times; persistent 401s still fail.
                status = getattr(exc, "status", None)
                detail = _error_text(exc).casefold()
                unauthorized = str(status) == "401" or "401" in detail or "unauthorized" in detail
                if unauthorized and attempt < MAXKB_INGEST_RETRY_LIMIT:
                    token_path = ROOT / ".local-services/maxkb_local/secrets/admin.token"
                    if token_path.exists():
                        refreshed = token_path.read_text(encoding="utf-8", errors="replace").strip()
                        if refreshed:
                            self.admin_key = refreshed
                    time.sleep(_maxkb_retry_delay(attempt))
                    continue
                if attempt >= MAXKB_INGEST_RETRY_LIMIT or not _maxkb_retryable_exception(exc):
                    raise
                time.sleep(_maxkb_retry_delay(attempt))
        raise EvalError(f"MAXKB_REQUEST_RETRY_EXHAUSTED:{operation}")

    def _discover_embedding_model(self) -> str:
        if self.embedding_model_id:
            return self.embedding_model_id
        models = list_items(self._admin("GET", "/workspace/default/model", operation="maxkb-discover-embedding-model"))
        candidates = [
            item for item in models
            if isinstance(item, dict)
            and str(item.get("provider") or "") == MAXKB_MAAS_PROVIDER
            and str(item.get("model_name", item.get("name", ""))).casefold() == self.context.profiles["maas"].embedding_model.casefold()
        ]
        if not candidates:
            candidates = [
                item for item in models
                if isinstance(item, dict)
                and str(item.get("provider") or "") == MAXKB_MAAS_PROVIDER
                and self.context.profiles["maas"].embedding_model.casefold() in str(item).casefold()
            ]
        if not candidates:
            raise ContractUnsupported("MAXKB_MAAS_BGE_M3_MODEL_NOT_REGISTERED")
        if len(candidates) > 1:
            raise ContractUnsupported(
                "MAXKB_MAAS_BGE_M3_MODEL_AMBIGUOUS_SET_MAXKB_EMBEDDING_MODEL_ID"
            )
        model_id = self._id(candidates[0], ("id", "model_id"))
        if not model_id:
            raise EvalError("MAXKB_EMBEDDING_MODEL_ID_MISSING")
        return model_id

    def _discover_chat_model(self) -> str:
        if self.chat_model_id:
            return self.chat_model_id
        models = list_items(self._admin("GET", "/workspace/default/model", operation="maxkb-discover-chat-model"))
        expected = self.context.profiles["llm"].model.casefold()
        candidates = [
            item
            for item in models
            if isinstance(item, dict)
            and str(item.get("provider") or "") == MAXKB_MAAS_PROVIDER
            and str(item.get("model_name", item.get("name", ""))).casefold() == expected
        ]
        if not candidates:
            candidates = [
                item for item in models
                if isinstance(item, dict)
                and str(item.get("provider") or "") == MAXKB_MAAS_PROVIDER
                and expected in str(item).casefold()
            ]
        if not candidates:
            raise ContractUnsupported("MAXKB_SELECTED_LLM_MODEL_NOT_REGISTERED")
        if len(candidates) > 1:
            raise ContractUnsupported(
                "MAXKB_SELECTED_LLM_MODEL_AMBIGUOUS_SET_MAXKB_LLM_MODEL_ID"
            )
        model_id = self._id(candidates[0], ("id", "model_id"))
        if not model_id:
            raise EvalError("MAXKB_CHAT_MODEL_ID_MISSING")
        return model_id

    def _create_knowledge(self, key: str) -> str:
        if self._configured_resource_allowed(key, self.configured_knowledge_id):
            return self.configured_knowledge_id
        model_id = self._discover_embedding_model()
        payload = self._admin("POST", "/workspace/default/knowledge/base", body={"name": _bounded_unique_name(f"CompetitorEval-{self.args.run_id}-{key}", 100), "folder_id": "default", "desc": "competitor-eval-ready-v1", "embedding_model_id": model_id}, operation=f"maxkb-create-knowledge-{_safe_name(key)}", timeout=self.args.upload_timeout)
        knowledge_id = self._id(payload, ("id", "knowledge_id"))
        if not knowledge_id:
            raise EvalError("MAXKB_CREATE_KNOWLEDGE_NO_ID")
        return knowledge_id

    def _create_native_app(self, key: str, knowledge_id: str) -> dict[str, Any]:
        # Compatibility entry point retained for callers inspecting the old
        # adapter shape. _ensure_native_app never invokes it for MaxKB 2.10.4.
        raise ContractUnsupported("MAXKB_NATIVE_THINKING_DISABLE_UNPROVEN")

        # Legacy native-app construction below is intentionally unreachable.
        chat_model_id = self._discover_chat_model()
        app = self._admin(
            "POST",
            "/workspace/default/application",
            body={
                "name": _bounded_unique_name(f"CompetitorEval-{self.args.run_id}-{key}", 64),
                "desc": "isolated competitor-eval-ready-v1 generative RAG",
                "folder_id": "default",
                "model_id": chat_model_id,
                "dialogue_number": 3,
                "prologue": "",
                "knowledge_id_list": [knowledge_id],
                "knowledge_setting": {
                    "top_n": int(self.args.top_k),
                    "similarity": 0.0,
                    "max_paragraph_char_number": 20000,
                    "search_mode": "embedding",
                    "no_references_setting": {"status": "designated_answer", "value": "No matching knowledge."},
                },
                "model_setting": {
                    "prompt": "Use only the following retrieved knowledge:\n{data}\n\nQuestion: {question}",
                    "system": "Grounded RAG assistant. Answer in the same language as the question.",
                    "no_references_prompt": "No relevant knowledge was retrieved.",
                },
                "problem_optimization": False,
                "type": "SIMPLE",
                "model_params_setting": {"temperature": 0.1, "max_tokens": 1024, "thinking": dict(THINKING_CONFIG)},
            },
            operation=f"maxkb-create-app-{_safe_name(key)}",
            timeout=self.args.upload_timeout,
        )
        app_id = self._id(app, ("id", "application_id", "app_id"))
        if not app_id:
            raise ContractUnsupported("MAXKB_CREATE_APPLICATION_NO_ID")
        published = self._admin(
            "PUT",
            f"/workspace/default/application/{app_id}/publish",
            body={},
            operation=f"maxkb-publish-app-{_safe_name(key)}",
            timeout=self.args.upload_timeout,
        )
        if isinstance(published, Mapping) and published.get("is_publish") is False:
            raise EvalError("MAXKB_APPLICATION_NOT_PUBLISHED")
        key_payload = self._admin(
            "POST",
            f"/workspace/default/application/{app_id}/application_key",
            operation=f"maxkb-create-app-key-{_safe_name(key)}",
            timeout=self.args.upload_timeout,
        )
        app_key = str(first_value(key_payload, ("secret_key", "api_key", "key", "token")) or "")
        if not app_key:
            raise ContractUnsupported("MAXKB_CREATE_APPLICATION_KEY_EMPTY")
        secret_path = self.context.write_secret(key, app_key)
        return {
            "status": "ready",
            "contract": "maxkb_published_openai_compatible_application",
            "app_id": app_id,
            "app_key_secret_path": secret_path,
            "bound_resource_id": knowledge_id,
            "chat_model_id": chat_model_id,
            "model": self.context.profiles["llm"].model,
            "provider": self.context.profiles["llm"].name,
            "thinking": dict(THINKING_CONFIG),
        }

    def _ensure_native_app(self, key: str, resource: dict[str, Any]) -> None:
        # MaxKB 2.10.4 cannot prove that nested GLM thinking settings survive
        # its native OpenAI model form. Keep retrieval usable, but make native
        # QA explicitly unreachable and route generation through MaaS.
        resource["native_app"] = {
            "status": "unsupported",
            "contract": MAXKB_EXTERNAL_QA_CONTRACT,
            "error": "MAXKB_NATIVE_THINKING_DISABLE_UNPROVEN",
            "bound_resource_id": resource.get("resource_id"),
        }
        resource["qa_contract"] = MAXKB_EXTERNAL_QA_CONTRACT
        resource["qa_comparability"] = MAXKB_QA_COMPARABILITY
        resource.pop("app_id", None)
        resource.pop("app_key_secret_path", None)

    def _create_document(self, knowledge_id: str, document: Document, ordinal: int) -> str:
        if document.artifact_path is None:
            raise EvalError(f"MAXKB_DOCUMENT_ARTIFACT_MISSING:{document.document_id}")
        content = document.artifact_path.read_text(encoding="utf-8", errors="replace")
        raw_limit = value_from(
            self.env,
            "MAXKB_PARAGRAPH_MAX_CHARS",
            default=str(MAXKB_PARAGRAPH_MAX_CHARS),
        )
        try:
            paragraph_limit = int(raw_limit)
        except (TypeError, ValueError) as exc:
            raise RunnerError(f"MAXKB_PARAGRAPH_MAX_CHARS_INVALID:{raw_limit}") from exc
        if paragraph_limit < 1:
            raise RunnerError(f"MAXKB_PARAGRAPH_MAX_CHARS_INVALID:{paragraph_limit}")
        parts = _split_maxkb_text(content, paragraph_limit)
        if not parts:
            raise EvalError(f"MAXKB_DOCUMENT_EMPTY:{document.document_id}")
        name = document.artifact_path.name
        paragraphs = [
            {
                "title": name if len(parts) == 1 else f"{name}#part-{index:04d}",
                "content": part,
            }
            for index, part in enumerate(parts, start=1)
        ]
        payload = self._admin(
            "PUT",
            f"/workspace/default/knowledge/{knowledge_id}/document/batch_create",
            body=[{"name": name, "paragraphs": paragraphs}],
            operation=f"maxkb-batch-create-document-{ordinal:04d}",
            timeout=self.args.upload_timeout,
        )
        document_id = self._id(payload, ("id", "document_id"))
        if not document_id:
            items = list_items(payload)
            document_id = self._id(items[0], ("id", "document_id")) if items else ""
        if not document_id:
            raise EvalError(f"MAXKB_CREATE_DOCUMENT_NO_ID:{document.document_id}")
        return document_id

    def _wait_ready(self, knowledge_id: str, document_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + float(self.args.index_timeout)
        last: dict[str, Any] = {}
        refreshes = 0
        pending_polls = 0
        while time.monotonic() < deadline:
            payload = self._admin("GET", f"/workspace/default/knowledge/{knowledge_id}/document/{document_id}", operation=f"maxkb-readiness-{document_id}", timeout=self.args.query_timeout)
            last = payload if isinstance(payload, dict) else {"payload": payload}
            status = str(last.get("status", last.get("state", "")))
            # MaxKB exposes the embedding task state in the final character:
            # 0=pending, 1=started, 2=success, 3=failure.  A document can be
            # A newly submitted task can spend a long time at nnn0 while the
            # local embedding worker is busy.  Refreshing it after only a few
            # polls races MaxKB's QueueOnce guard and returns
            # "task-busy" (which is then incorrectly recorded as a failed
            # resource).  Wait for pending/started tasks; only an explicit
            # nnn3 failure is safe to requeue automatically.
            if status.endswith("0"):
                pending_polls += 1
                time.sleep(float(self.args.poll_seconds))
                continue
            elif status.endswith("3"):
                pending_polls = 0
                state_list = ["3"]
            else:
                pending_polls = 0
                state_list = None
            if state_list is not None:
                if refreshes >= MAXKB_INDEX_REFRESH_LIMIT:
                    raise EvalError(f"MAXKB_INDEX_FAILED:{status}")
                refreshes += 1
                self.context.progress_emit(
                    "ingest",
                    "MaxKB retrying failed embedding paragraphs",
                    document_id=document_id,
                    refresh=refreshes,
                )
                try:
                    self._admin(
                        "PUT",
                        f"/workspace/default/knowledge/{knowledge_id}/document/{document_id}/refresh",
                        body={"state_list": state_list},
                        operation=f"maxkb-refresh-failed-{document_id}-{refreshes}",
                        timeout=self.args.upload_timeout,
                    )
                except Exception as exc:
                    # QueueOnce may report that a refresh is already running
                    # even though the preceding readiness GET observed nnn3.
                    # Treat that response as an in-flight backend task and
                    # keep polling; turning it into a resource failure loses
                    # a durable upload and needlessly blocks the whole run.
                    if _maxkb_retryable_exception(exc):
                        time.sleep(float(self.args.poll_seconds))
                        continue
                    raise
                time.sleep(float(self.args.poll_seconds))
                continue
            if status.endswith("2") or status.casefold() in {"completed", "ready", "indexed", "available"}:
                return {"status": status, "failed_embedding_refreshes": refreshes}
            time.sleep(float(self.args.poll_seconds))
        raise EvalError(f"MAXKB_INDEX_TIMEOUT:{last}")

    def _ingest_resource(self, key: str, candidates: list[Document]) -> str:
        existing = self._existing(key) or {
            "resource_key": key,
            "document_ids": [document.document_id for document in candidates],
            "documents": {},
        }
        try:
            artifacts, unsupported = self._ingest_selection(key, candidates, existing)
            self._save_resource(key, existing)
            if not artifacts:
                existing.update({"status": "unsupported", "error": "NO_SUPPORTED_DOCUMENTS", "resource_id": None})
                self._save_resource(key, existing)
                return "UNSUPPORTED"

            # A resumed document-local scope whose uploads and native app are
            # already ready is durable. Do not transition it back to
            # ``starting`` and re-poll every paragraph on a later ingest pass;
            # that needlessly reopens completed scopes while sibling retries
            # run and can make the resource map appear to regress.
            if self._target_is_usable(existing) and isinstance(existing.get("uploads"), dict):
                self._ensure_native_app(key, existing)
                self._save_resource(key, existing)
                return "UNSUPPORTED" if str(existing.get("status")) == "partial" else "READY"

            existing.update({"status": "starting", "ready": False})
            knowledge_id = str(
                existing.get("knowledge_id")
                or existing.get("resource_id")
                or self._create_knowledge(key)
            )
            existing.update({"knowledge_id": knowledge_id, "resource_id": knowledge_id})
            existing.setdefault(
                "resource_origin",
                "configured_opt_in"
                if self._configured_resource_allowed(key, self.configured_knowledge_id)
                and knowledge_id == self.configured_knowledge_id
                else "created_for_run",
            )
            if not isinstance(existing.get("uploads"), dict):
                existing["uploads"] = {}
            self._save_resource(key, existing)

            # A global scope (for example MultiHop-RAG) contains hundreds of
            # independent documents in one MaxKB knowledge base.  Keep the
            # historical sequential behavior by default, but allow an
            # explicit bounded document-level fan-out for providers where the
            # single-resource loop would otherwise take hours.  Every upload
            # is checkpointed before readiness polling, so an interruption
            # still resumes without creating duplicates.
            resource_lock = threading.Lock()

            def ingest_one(item: tuple[int, Document]) -> None:
                ordinal, document = item
                with resource_lock:
                    state = existing["uploads"].get(document.document_id, {})
                    state = dict(state) if isinstance(state, Mapping) else {}
                document_id = str(
                    state.get("document_id")
                    or self._create_document(knowledge_id, document, ordinal)
                )
                with resource_lock:
                    existing["status"] = "indexing"
                    existing["uploads"][document.document_id] = {
                        **state,
                        "status": "submitted",
                        "document_id": document_id,
                        "sha256": document.source_hash,
                    }
                    # A successful batch_create is durable before readiness
                    # polling so a failed poll resumes without a duplicate.
                    self._save_resource(key, existing)
                if state.get("status") == "ready" and state.get("index"):
                    index = state["index"]
                else:
                    index = self._wait_ready(knowledge_id, document_id)
                with resource_lock:
                    existing["uploads"][document.document_id].update({"status": "ready", "index": index})
                    self._save_resource(key, existing)

            global_parallelism = int(value_from(self.env, "MAXKB_GLOBAL_DOCUMENT_CONCURRENCY", default="1") or 1)
            if self.package.scope == "global" and global_parallelism > 1 and len(artifacts) > 1:
                failures: list[BaseException] = []
                with ThreadPoolExecutor(
                    max_workers=min(global_parallelism, len(artifacts)),
                    thread_name_prefix="maxkb-global-ingest",
                ) as executor:
                    futures = [
                        executor.submit(ingest_one, (ordinal, document))
                        for ordinal, document in enumerate(artifacts, start=1)
                    ]
                    for future in futures:
                        try:
                            future.result()
                        except BaseException as exc:  # preserve ready checkpoints from sibling documents
                            failures.append(exc)
                if failures:
                    raise failures[0]
            else:
                for ordinal, document in enumerate(artifacts, start=1):
                    ingest_one((ordinal, document))

            self._mark_candidates_ready(existing, candidates, unsupported)
            existing.update(
                {
                    "status": "partial" if unsupported else "ready",
                    "ready": True,
                    "embedding_model_id": self.embedding_model_id or "discovered",
                }
            )
            # A resumed ingest may recover from a transient MaxKB task-busy
            # response. Do not retain that historical error on a resource
            # which is now fully indexed.
            existing.pop("error", None)
            self._save_resource(key, existing)
            self._ensure_native_app(key, existing)
            self._save_resource(key, existing)
            return "UNSUPPORTED" if unsupported else "READY"
        except Exception as exc:
            status = _exception_status(exc)
            existing.update(
                {
                    "status": "blocked" if status == "BLOCKED" else "failed",
                    "ready": False,
                    "error": _error_text(exc),
                }
            )
            self._save_resource(key, existing)
            return status

    def ingest(self) -> dict[str, Any]:
        targets = list(self._target_resources())
        statuses: list[str] = []
        if targets:
            with ThreadPoolExecutor(
                max_workers=min(int(self.args.maxkb_ingest_concurrency), len(targets)),
                thread_name_prefix="maxkb-ingest",
            ) as executor:
                futures = [
                    executor.submit(self._ingest_resource, key, candidates)
                    for key, candidates in targets
                ]
                # Consume futures in package order.  Workers may finish in any
                # order, but counts and the returned resource map stay stable.
                statuses = [future.result() for future in futures]

        counts: Counter[str] = Counter()
        for status in statuses:
            counts[status] += 1
        payload = self._resources_payload()
        resources = payload.get("resources", {})
        if isinstance(resources, dict):
            ordered_resources = {
                key: resources[key]
                for key, _ in targets
                if key in resources
            }
            ordered_resources.update(
                {key: resources[key] for key in sorted(resources) if key not in ordered_resources}
            )
            payload["resources"] = ordered_resources
            self.context.save_resources(payload)
        status = "SUCCESS" if not any(
            counts.get(value) for value in ("FAILED", "BLOCKED", "TIMEOUT", "INTERRUPTED")
        ) else "PARTIAL"
        return {"status": status, "resource_counts": dict(counts), "resources": payload.get("resources", {})}

    def retrieve(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        self._supported("retrieval", question.media)
        knowledge_id = str(resource.get("knowledge_id") or resource.get("resource_id") or "")
        if not knowledge_id:
            raise RunnerError("MAXKB_KNOWLEDGE_ID_MISSING")
        payload = self._admin(
            "POST",
            f"/workspace/default/knowledge/{knowledge_id}/hit_test",
            body={"query_text": question.text, "top_number": int(self.args.top_k), "similarity": 0.0, "search_mode": "embedding"},
            operation=f"maxkb-admin-hit-test-{_safe_name(question.question_id)}",
            timeout=self.args.query_timeout,
        )
        hits = list_items(payload)
        return {"contract": "diagnostic_admin_contract", "payload": payload, "hits": hits[: int(self.args.top_k)]}

    def _native_client_and_path(self, resource: Mapping[str, Any]) -> tuple[Any, str, str]:
        app_id = str(resource.get("app_id") or "")
        if not app_id:
            raise ProviderUnavailable("MAXKB_RESOURCE_APPLICATION_ID_MISSING")
        app_key = self.context.read_secret(resource.get("app_key_secret_path"))
        client = self.context._http(self.public_base, timeout=self.args.qa_timeout)
        return client, f"/{app_id}/chat/completions", app_key

    def qa(self, question: Question, resource: dict[str, Any]) -> dict[str, Any]:
        self._supported("native_qa", question.media)
        retrieval = self.retrieve(question, resource)
        hits = retrieval.get("hits", [])
        try:
            context_chars = int(
                value_from(
                    self.env,
                    "MAXKB_EXTERNAL_CONTEXT_CHARS",
                    default=str(MAXKB_EXTERNAL_CONTEXT_CHARS),
                )
            )
        except (TypeError, ValueError) as exc:
            raise RunnerError("MAXKB_EXTERNAL_CONTEXT_CHARS_INVALID") from exc
        if context_chars < 1:
            raise RunnerError("MAXKB_EXTERNAL_CONTEXT_CHARS_INVALID")
        try:
            context_chunks = int(
                value_from(
                    self.env,
                    "MAXKB_EXTERNAL_MAX_CHUNKS",
                    default=str(MAXKB_EXTERNAL_MAX_CHUNKS),
                )
            )
        except (TypeError, ValueError) as exc:
            raise RunnerError("MAXKB_EXTERNAL_MAX_CHUNKS_INVALID") from exc
        if context_chunks < 1:
            raise RunnerError("MAXKB_EXTERNAL_MAX_CHUNKS_INVALID")
        retrieved_context = _maxkb_external_context(
            hits,
            question.text,
            max_chars=context_chars,
            max_chunks=context_chunks,
        )
        prompt = f"{SHARED_PROMPT_TEXT}\n\nRetrieved context:\n{retrieved_context}\n\nQuestion: {question.text}"
        self._qa_rate_limiter.wait_for_slot()
        client = self.context._http(self.context.profiles["llm"].base_url, timeout=self.args.qa_timeout)
        payload = client.request(
            "POST",
            "/chat/completions",
            api_key=self.context.profiles["llm"].api_key,
            json_body={
                "model": self.context.profiles["llm"].model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "temperature": 0,
                "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
                "thinking": dict(THINKING_CONFIG),
            },
            operation=f"maxkb-external-maas-qa-{_safe_name(question.question_id)}",
            timeout=self.args.qa_timeout,
        )
        return {
            "contract": MAXKB_EXTERNAL_QA_CONTRACT,
            "comparability": MAXKB_QA_COMPARABILITY,
            "retrieval_contract": retrieval.get("contract", "diagnostic_admin_contract"),
            "diagnostic_hits": hits,
            "payload": payload,
            "answer": answer_from(payload),
            "generation_provider": self.context.profiles["llm"].name,
            "generation_model": self.context.profiles["llm"].model,
            "thinking": dict(THINKING_CONFIG),
            "prompt_version": SHARED_PROMPT_VERSION,
            "prompt_hash": SHARED_PROMPT_HASH,
        }


def _write_json_map(path: Path, payload: dict[str, Any]) -> None:
    json_dump(path, payload)


def _add_common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--system", choices=SYSTEMS, required=True)
    parser.add_argument("--package", type=Path, required=True, help="competitor-eval-ready-v1 directory or manifest")
    parser.add_argument("--output-root", type=Path, default=ROOT / "runs/stage1")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--dry-run", action="store_true", help="write and validate artifacts without making any network request")
    parser.add_argument(
        "--reuse-configured-resource",
        action="store_true",
        help="explicitly allow a configured global dataset/knowledge ID; native apps are still created fresh and bound per resource",
    )
    parser.add_argument("--repeats", type=int, default=int(CONTRACTS["defaults"]["repeats"]))
    parser.add_argument(
        "--qa-concurrency",
        "--concurrency",
        dest="qa_concurrency",
        type=int,
        default=None,
        help="bounded concurrent QA sessions (default 4; env COMPETITOR_EVAL_QA_CONCURRENCY)",
    )
    parser.add_argument(
        "--retrieval-concurrency",
        type=int,
        default=None,
        help="bounded concurrent retrieval requests (default 8; env COMPETITOR_EVAL_RETRIEVAL_CONCURRENCY)",
    )
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--service-timeout", type=float, default=float(CONTRACTS["defaults"]["timeout_seconds"]["service_readiness"]))
    parser.add_argument("--provider-timeout", type=float, default=float(CONTRACTS["defaults"]["timeout_seconds"]["provider_probe"]))
    parser.add_argument("--upload-timeout", type=float, default=float(CONTRACTS["defaults"]["timeout_seconds"]["ingest_upload"]))
    parser.add_argument("--index-timeout", type=float, default=float(CONTRACTS["defaults"]["timeout_seconds"]["index_wait"]))
    parser.add_argument("--query-timeout", type=float, default=float(CONTRACTS["defaults"]["timeout_seconds"]["retrieval"]))
    parser.add_argument("--qa-timeout", type=float, default=float(CONTRACTS["defaults"]["timeout_seconds"]["qa"]))
    parser.add_argument(
        "--dify-max-indexing-scopes",
        "--max-indexing-scopes",
        dest="dify_max_indexing_scopes",
        type=int,
        default=None,
        help="Dify document-local scopes submitted before readiness polling (default 3, maximum 8; env DIFY_MAX_INDEXING_SCOPES)",
    )
    parser.add_argument(
        "--maxkb-ingest-concurrency",
        "--maxkb-concurrency",
        dest="maxkb_ingest_concurrency",
        type=int,
        default=None,
        help="bounded concurrent MaxKB resource ingests (default 2; env MAXKB_INGEST_CONCURRENCY)",
    )
    parser.add_argument(
        "--fastgpt-ingest-concurrency",
        "--fastgpt-concurrency",
        dest="fastgpt_ingest_concurrency",
        type=int,
        default=None,
        help="bounded concurrent FastGPT resource ingests (default 2; env FASTGPT_INGEST_CONCURRENCY)",
    )
    parser.add_argument("--qianfan-base-url", default="", help="legacy checkpoint compatibility only; never materialized")
    parser.add_argument("--maas-base-url", default="")
    parser.add_argument("--deepseek-base-url", default="")
    parser.add_argument(
        "--text-llm-provider",
        "--llm-provider",
        choices=("qianfan", "maas", "deepseek", "deepseek-official", "dsv4f"),
        default="",
        help="selected text generation provider (deepseek/deepseek-official/dsv4f aliases are admitted; legacy values fail closed)",
    )
    parser.add_argument("--qianfan-llm-model", default="", help="legacy checkpoint compatibility only; never materialized")
    parser.add_argument("--qianfan-image-llm-model", default="", help="legacy compatibility only; text-only image model is NOT_APPLICABLE")
    parser.add_argument("--deepseek-llm-model", "--llm-model", dest="deepseek_llm_model", default="")
    parser.add_argument("--maas-llm-model", default="", help="legacy checkpoint compatibility only; never materialized")
    parser.add_argument("--maas-vl-model", default="", help="legacy option; derived text-only package freezes image model as NOT_APPLICABLE")
    parser.add_argument("--maas-embedding-model", default="")
    parser.add_argument("--maas-embedding-dimension", type=int, default=0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for stage in STAGES:
        subparser = subparsers.add_parser(stage, help=f"run the {stage} stage")
        _add_common_arguments(subparser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.top_k < 1:
        parser.error("--repeats and --top-k must be positive")
    try:
        package = EvalPackage.load(args.package)
        context = RunnerContext(args, package)
        result = context.run()
        print(json.dumps(redact(result), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("status") in {"SUCCESS", "PARTIAL", "DRY_RUN"} or args.command == "preflight" else 1
    except (PackageError, RunnerError, EvalError) as exc:
        print(json.dumps({"status": "ERROR", "error": _error_text(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
