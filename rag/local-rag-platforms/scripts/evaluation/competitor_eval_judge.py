#!/usr/bin/env python3
"""Safe, resumable LLM-judge/scorer layer for completed competitor runs.

The generic competitor runner owns product requests and its initial/terminal
ledgers.  This module consumes those immutable artifacts plus one frozen
``competitor-eval-ready-v1`` condition package.  It deliberately has a small
boundary:

* it never downloads a dataset or an image;
* it admits only the frozen DeepSeek OpenAI-compatible
  ``deepseek-v4-flash`` text judge using ``DEEPSEEK_API_KEY_NEW``;
* it creates its own hashed start record and denominator before the first
  judge request;
* retries stay inside one judge unit and a terminal ledger row is appended
  only after the unit reaches a terminal disposition; and
* absent context, citations, or visual assets remain explicit unsupported
  values rather than being inferred from an answer string.

The public seams are :class:`JudgeRunner`, :func:`preflight`, :func:`run`,
:func:`aggregate`, and the ``preflight``/``run``/``aggregate`` CLI commands.
Only the Python standard library and the repository's existing
``ArtifactHTTP`` helper are used.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import importlib.metadata
import json
import math
import mimetypes
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

try:  # The shared helper is optional for import-time test isolation.
    from mmdocir_competitor_eval import ArtifactHTTP, Progress, redact as _shared_redact  # type: ignore
except ImportError:  # pragma: no cover - the normal checkout has the helper
    ArtifactHTTP = None  # type: ignore[assignment,misc]
    Progress = None  # type: ignore[assignment,misc]
    _shared_redact = None

from competitor_eval_metric_registry import (  # noqa: E402
    normalize_structured_gold,
    structured_gold_complete,
)


SCHEMA = "competitor-eval-judge-v1"
RESPONSE_SCHEMA = "competitor-eval-judge-response-v1"
PACKAGE_SCHEMA = "competitor-eval-ready-v1"
TEXT_JUDGE_MODEL = "deepseek-v4-flash"
JUDGE_MODEL_VERSION = "deepseek-v4-flash"
EMBEDDING_MODEL = "bge-m3"
EMBEDDING_PROVIDER = "maas"
JUDGE_PROVIDER = "deepseek-official"
JUDGE_API_KEY_ENV = "DEEPSEEK_API_KEY_NEW"
DEFAULT_JUDGE_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_HOST = "api.deepseek.com"
MIXED_DATASET_ID = "moi-rag-bench-v0.1-text-only-no-mllm"
TEXT_ONLY_READINESS_SMOKE_DATASET_ID = "moi-rag-bench-v0.1-text-only-no-mllm-readiness-smoke"
LEGACY_MIXED_DATASET_IDS = (
    "moi-rag-bench-v0.1-ready-for-eval",
    "moi-rag-bench-v0.1-mixed",
    "moi-rag-bench-v0.2",
    "moi-rag-bench-v0.2-ready-for-eval",
    "moi-rag-bench-v0.2-text-only-no-mllm",
    "moi-rag-bench-v0.3-final",
    "moi-rag-bench-v0.3-final-ready-for-eval",
    "moi-rag-bench-v0.3-final-text-only-no-mllm",
    "moi-rag-bench-v0.3.1-qa-revision",
    "moi-rag-bench-v0.3.1-new-qa-increment",
)
MIXED_RUBRIC_VERSION = "moi-rag-bench-v0.1-adapted-reference-rubric-v1"
PROMPT_VERSION = "moi-rag-bench-v0.1-judge-prompt-v1"
TEXT_ONLY_CONDITION = "text-only-no-mllm"
RAGAS_COMPATIBLE_JUDGE = "RAGAS_COMPATIBLE_JUDGE"
RAGAS_EXACT = "RAGAS_EXACT_0.2.15"
DEFAULT_CONCURRENCY = 1
DEFAULT_RETRIES = 2
DEFAULT_TIMEOUT = 180.0
EXCLUDED_DATASETS = frozenset({"omnidocbench", "omnidocbench-bench", "lenovo", "lenovo-bench"})
# The Huawei MaaS Dify plugin is namespaced under ``matrixorigin`` too.  Only
# reject retired/non-MaaS provider or model identifiers, not every
# MatrixOrigin-owned provider string encountered in a local run record.
TAAS_MARKERS = (
    "taas",
    "matrixorigin_taas",
    "matrixorigin.cn",
    "qianfan",
    "baidubce",
    "qwen",
)


def _valid_judge_base_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme.casefold() == "https"
        and (parsed.hostname or "").casefold() == DEEPSEEK_HOST
        and port in (None, 443)
        and not parsed.username
        and not parsed.password
    )
FAILURE_STATUSES = frozenset(
    {
        "FAILED",
        "FAILURE",
        "ERROR",
        "BLOCKED",
        "TIMEOUT",
        "TIMED_OUT",
        "INTERRUPTED",
        "ABORTED",
        "CANCELLED",
        "CANCELED",
        "REJECTED",
    }
)
UNSUPPORTED_STATUSES = frozenset({"UNSUPPORTED", "NOT_SUPPORTED"})
TERMINAL_STATUSES = FAILURE_STATUSES | UNSUPPORTED_STATUSES | frozenset({"SUCCESS", "EMPTY"})


DATASET_ALIASES = {
    "wiki": "wikieval",
    "wiki-eval": "wikieval",
    "wikieval": "wikieval",
    "mm-doc-ir": "mmdocir",
    "mm_doc_ir": "mmdocir",
    "mmdocir": "mmdocir",
    "mm-doc-rag": "mmdocrag",
    "mm_doc_rag": "mmdocrag",
    "mmdocrag": "mmdocrag",
    "doc-bench": "docbench",
    "doc_bench": "docbench",
    "docbench": "docbench",
    "multi-hop": "multihop-rag",
    "multihop": "multihop-rag",
    "multihoprag": "multihop-rag",
    "multihop-rag": "multihop-rag",
    "enterprise": "enterprise-rag-bench",
    "enterprise-rag": "enterprise-rag-bench",
    "enterpriserag": "enterprise-rag-bench",
    "enterpriserag-bench": "enterprise-rag-bench",
    "enterprise-rag-bench": "enterprise-rag-bench",
    "fab": "fab-bench",
    "fabbench": "fab-bench",
    "fab-bench": "fab-bench",
    "omnidocbench": "omnidocbench",
    "omni-doc-bench": "omnidocbench",
    "lenovo": "lenovo-bench",
    "lenovo-bench": "lenovo-bench",
    MIXED_DATASET_ID: MIXED_DATASET_ID,
    TEXT_ONLY_READINESS_SMOKE_DATASET_ID: MIXED_DATASET_ID,
    **{legacy_id: MIXED_DATASET_ID for legacy_id in LEGACY_MIXED_DATASET_IDS},
}


MIXED_DIMENSION_NAMES = (
    "response_claim_correctness",
    "reference_claim_recall",
    "critical_claim_coverage",
    "gold_evidence_support",
    "runtime_context_faithfulness",
    "strict_unanswerable",
    "false_refusal",
    "answer_relevance",
    "instruction_compliance",
    "contradiction_free",
    "unsupported_claim_rate",
)
MIXED_CANONICAL_DIMENSION_NAMES = frozenset(
    {
        "reference_claim_recall",
        "critical_claim_coverage",
        "gold_evidence_support",
    }
)
MIXED_ANSWERABLE_ONLY_DIMENSION_NAMES = frozenset(
    {
        "response_claim_correctness",
        "reference_claim_recall",
        "critical_claim_coverage",
        "gold_evidence_support",
        "runtime_context_faithfulness",
        "false_refusal",
    }
)
MIXED_UNANSWERABLE_ONLY_DIMENSION_NAMES = frozenset({"strict_unanswerable"})

MIXED_SOURCE_ALIASES = {
    "docbench": "docbench",
    "doc-bench": "docbench",
    "enterprise": "enterprise",
    "enterprise-rag-bench": "enterprise",
    "enterpriserag-bench": "enterprise",
    "multihop": "multihop",
    "multi-hop": "multihop",
    "multihop-rag": "multihop",
    "mmdocir": "mmdocir",
    "mm-doc-ir": "mmdocir",
}

MIXED_RUBRICS = {
    "docbench": {
        "answerable": "DocBench adapted text-projection correctness; metadata and text-only questions use the frozen reference/evidence, while multimodal-text-projection remains non-visual.",
        "unanswerable": "DocBench adapted text-projection strict refusal; do not treat missing reference text as an answer.",
        "question_types": {
            "metadata": "Prefer exact metadata facts and do not infer absent page/layout fields.",
            "multimodal": "Judge only the supplied text projection; never claim native visual understanding.",
            "multimodal-t": "Judge only the supplied text projection; never claim native visual understanding.",
        },
    },
    "enterprise": {
        "answerable": "Enterprise adapted correctness/completeness; preserve source scope and conflict/constrained facts.",
        "unanswerable": "Enterprise adapted info-not-found/strict refusal; no guessed enterprise fact.",
        "question_types": {
            "conflict": "Resolve only explicit frozen evidence; mark contradictions instead of smoothing them away.",
            "constrained": "Follow requested constraints and report omissions.",
        },
    },
    "multihop": {
        "answerable": "MultiHop adapted multi-evidence correctness; require all stated hops when structured Gold is available.",
        "unanswerable": "MultiHop adapted null/strict refusal; do not complete an absent hop from prior knowledge.",
        "question_types": {
            "comparison": "Check both sides of the comparison and preserve the requested relation.",
            "inference": "Check the inference chain against supplied evidence only.",
            "temporal": "Check dates and temporal direction explicitly.",
        },
    },
    "mmdocir": {
        "answerable": "MMDocIR adapted QA correctness over the text projection; retrieval/page/layout claims require explicit trace.",
        "unanswerable": "MMDocIR adapted non-answer/refusal; absence of a reference answer is not permission to guess.",
        "question_types": {
            "text-only": "Check the answer against the available text evidence.",
            "single_doc_single_evidence": "Check the single document/evidence relation without inventing page trace.",
        },
    },
}


class JudgeError(RuntimeError):
    """A safe, auditable judge-layer error."""


class PackageError(JudgeError):
    pass


class RunError(JudgeError):
    pass


class ProviderError(JudgeError):
    pass


class SchemaError(JudgeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_value(value: Any) -> str:
    return f"sha256:{sha256_bytes(_canonical(value))}"


def _hash_file(path: Path) -> str:
    return f"sha256:{sha256_bytes(path.read_bytes())}"


def _safe_slug(value: Any, limit: int = 100) -> str:
    result = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value)).strip("._") or "item"
    return result[:limit]


def _norm_dataset(value: Any) -> str:
    raw = str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")
    compact = re.sub(r"[^a-z0-9-]+", "", raw)
    return DATASET_ALIASES.get(raw, DATASET_ALIASES.get(compact, compact))


def _redact_text(value: str, secrets: Iterable[str] = ()) -> str:
    result = str(value)
    for secret in sorted({str(item) for item in secrets if str(item)}, key=len, reverse=True):
        result = result.replace(secret, "<redacted>")
    result = re.sub(r"(?i)\bBearer\s+[^\s,;]+", "Bearer <redacted>", result)
    result = re.sub(r"(?i)\b(?:sk|key|token)[-_][A-Za-z0-9._-]{8,}", "<redacted>", result)
    return result


def redact(value: Any, key: str | None = None, *, secrets: Iterable[str] = ()) -> Any:
    """Redact secrets without changing the shape of an artifact."""

    lowered = str(key or "").casefold()
    if any(marker in lowered for marker in ("api_key", "apikey", "access_token", "refresh_token", "authorization", "password", "secret")):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(name): redact(child, str(name), secrets=secrets) for name, child in value.items()}
    if isinstance(value, list):
        return [redact(child, secrets=secrets) for child in value]
    if isinstance(value, tuple):
        return [redact(child, secrets=secrets) for child in value]
    if isinstance(value, str):
        return _redact_text(value, secrets)
    return value


def _redacted(value: Any, secrets: Iterable[str] = ()) -> Any:
    return redact(value, secrets=secrets)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    temporary.write_bytes(content)
    with temporary.open("rb") as handle:
        os.fsync(handle.fileno())
    temporary.replace(path)


def _write_hash_sidecar(path: Path) -> None:
    path.with_suffix(path.suffix + ".sha256").write_text(
        f"{sha256_bytes(path.read_bytes())}  {path.name}\n", encoding="utf-8"
    )


def _write_json(path: Path, value: Any, *, secrets: Iterable[str] = ()) -> None:
    encoded = (json.dumps(_redacted(value, secrets), ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, encoded)
    _write_hash_sidecar(path)


def _append_jsonl(path: Path, rows: Iterable[Mapping[str, Any]], *, secrets: Iterable[str] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        for row in rows:
            handle.write((json.dumps(_redacted(dict(row), secrets), ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    _write_hash_sidecar(path)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except FileNotFoundError as exc:
        raise RunError(f"FILE_MISSING:{path}") from exc
    except json.JSONDecodeError as exc:
        raise RunError(f"JSON_INVALID:{path}") from exc


def _read_jsonl(path: Path, *, required: bool = True) -> list[dict[str, Any]]:
    if not path.exists():
        if required:
            raise RunError(f"FILE_MISSING:{path}")
        return []
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RunError(f"JSONL_INVALID:{path}:{line_number}") from exc
        if isinstance(item, Mapping):
            rows.append(dict(item))
        else:
            raise RunError(f"JSONL_ROW_NOT_OBJECT:{path}:{line_number}")
    return rows


def _verify_sidecar(path: Path) -> bool:
    sidecar = path.with_suffix(path.suffix + ".sha256")
    if not sidecar.exists():
        return False
    expected = sidecar.read_text(encoding="utf-8", errors="replace").strip().split()
    return bool(expected and expected[0] == sha256_bytes(path.read_bytes()))


def _first(mapping: Mapping[str, Any] | None, *names: str, default: Any = None) -> Any:
    if mapping is None:
        return default
    for name in names:
        if name in mapping and mapping[name] not in (None, ""):
            return mapping[name]
    return default


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, (tuple, set)):
        return list(value)
    return [value]


def _parse_attempt_id(value: Any) -> tuple[str, int]:
    text = str(value or "")
    match = re.match(r"^(.*?)#repeat-(\d+)$", text)
    if match:
        return match.group(1), int(match.group(2))
    return text, 1


def _unit_key(question_id: Any, repeat_id: Any = 1) -> tuple[str, int]:
    qid = str(question_id or "")
    try:
        repeat = int(repeat_id or 1)
    except (TypeError, ValueError):
        repeat = 1
    return qid, repeat


def _status(value: Any) -> str:
    return str(value or "").strip().upper() or "UNKNOWN"


def _is_url(value: str) -> bool:
    return urlsplit(value).scheme in {"http", "https"}


def _is_data_image(value: str) -> bool:
    return bool(re.match(r"^data:image/[^;]+;base64,[A-Za-z0-9+/=]+$", value.strip()))


def _json_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        text_parts = []
        for item in value:
            if isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}:
                text_parts.append(str(item.get("text") or item.get("content") or ""))
            elif isinstance(item, str):
                text_parts.append(item)
        return "".join(text_parts)
    return str(value or "")


def _strip_json_fence(value: str) -> str:
    stripped = value.strip()
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", stripped, flags=re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else stripped


def _content_from_chat_response(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
            message = choices[0].get("message")
            if isinstance(message, Mapping) and "content" in message:
                return message["content"]
            if "text" in choices[0]:
                return choices[0]["text"]
        for key in ("content", "output", "result", "response"):
            if key in payload and isinstance(payload[key], (str, Mapping, list)):
                return payload[key]
    return payload


def _usage_from_chat_response(payload: Any) -> dict[str, Any]:
    if isinstance(payload, Mapping) and isinstance(payload.get("usage"), Mapping):
        return {str(key): value for key, value in payload["usage"].items()}
    return {}


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _scale_max(spec: Mapping[str, Any]) -> float:
    scale = spec.get("scale", [0, 1])
    return float(scale[1]) if isinstance(scale, Sequence) and len(scale) > 1 else 1.0


def _dimension(name: str, maximum: int | float, *, requires: Sequence[str] = (), description: str = "") -> dict[str, Any]:
    return {"name": name, "scale": [0, maximum], "requires": list(requires), "description": description}


def _contracts() -> dict[str, dict[str, Any]]:
    return {
        "wikieval": {
            "dataset": "WikiEval",
            "protocol_tag": RAGAS_COMPATIBLE_JUDGE,
            "implementation": "RAGAS-compatible LLM judge; exact ragas==0.2.15 is not claimed",
            "dimensions": {
                "faithfulness": _dimension("faithfulness", 1, requires=("actual_context",), description="Every material answer claim is entailed by actual retrieved context."),
                "answer_relevance": _dimension("answer_relevance", 1, description="The answer directly addresses the question."),
                "context_precision": _dimension("context_precision", 1, requires=("actual_context", "gold_evidence"), description="Retrieved context is relevant and ranked usefully."),
                "context_recall": _dimension("context_recall", 1, requires=("actual_context", "gold_evidence"), description="Retrieved context covers the frozen reference evidence."),
            },
            "slices": ["question_type"],
            "embedding": {"required": False, "provider": EMBEDDING_PROVIDER, "model": EMBEDDING_MODEL, "only_if_exact_metric": True},
        },
        "mmdocir": {
            "dataset": "MMDocIR",
            "protocol_tag": "MMDocIR_ADAPTED_QA_JUDGE",
            "implementation": "project-adapted QA judge; retrieval metrics remain in the runner",
            "dimensions": {
                "answer_correctness": _dimension("answer_correctness", 1, description="Answer agrees with frozen reference answer and required facts."),
                "faithfulness": _dimension("faithfulness", 1, requires=("actual_context",), description="Answer claims are supported by actual retrieved context."),
                "citation_support": _dimension("citation_support", 1, requires=("actual_context", "citations"), description="Submitted citations resolve and support the answer."),
            },
            "slices": ["question_type", "evidence_type"],
            "embedding": {"required": False, "provider": EMBEDDING_PROVIDER, "model": EMBEDDING_MODEL, "only_if_exact_metric": True},
        },
        "mmdocrag": {
            "dataset": "MMDocRAG",
            "protocol_tag": "MMDOC-RAG_ADAPTED_JUDGE",
            "implementation": "five paper-native 0-5 judge dimensions plus explicit quote support",
            "dimensions": {
                "fluency": _dimension( "fluency", 5, description="Answer is clear and linguistically coherent."),
                "citation_quality": _dimension("citation_quality", 5, requires=("citations",), description="Citations are present, resolvable, and appropriately placed."),
                "text_image_coherence": _dimension("text_image_coherence", 5, requires=("image_evidence",), description="Text and visual evidence are used consistently when visual evidence is present."),
                "reasoning_logic": _dimension("reasoning_logic", 5, requires=("actual_context",), description="Reasoning follows from the supplied evidence."),
                "factuality": _dimension("factuality", 5, requires=("actual_context",), description="Claims are factually supported by the supplied evidence."),
                "quote_support": _dimension("quote_support", 1, requires=("actual_context", "gold_evidence"), description="Selected/quoted evidence supports the answer and matches Gold quote support."),
            },
            "slices": ["question_type", "evidence_modality", "candidate_pool"],
            "embedding": {"required": False, "provider": EMBEDDING_PROVIDER, "model": EMBEDDING_MODEL, "only_if_exact_metric": True},
        },
        "docbench": {
            "dataset": "DocBench",
            "protocol_tag": "DOCBENCH_OFFICIAL_BINARY_CORRECTNESS",
            "implementation": "official binary correctness rubric, reported overall and by question type",
            "dimensions": {
                "correctness_binary": _dimension("correctness_binary", 1, description="Official DocBench binary correctness decision."),
            },
            "rubric_by_type": {
                "text-only": "1 only when the answer is materially correct for the text question; otherwise 0.",
                "multimodal": "1 only when the answer correctly uses the required PDF visual/table/layout evidence; otherwise 0.",
                "metadata": "1 only when the requested document metadata is correctly identified; otherwise 0.",
                "unanswerable": "1 only for a justified refusal when the frozen item is unanswerable; unsupported guessing is 0.",
            },
            "slices": ["question_type", "domain"],
            "embedding": {"required": False, "provider": EMBEDDING_PROVIDER, "model": EMBEDDING_MODEL, "only_if_exact_metric": True},
        },
        "multihop-rag": {
            "dataset": "MultiHop-RAG",
            "protocol_tag": "MULTIHOP_ADAPTED_CONTEXT_JUDGE",
            "implementation": "actual-context correctness, Gold-reference correctness, and answer faithfulness, by question type",
            "dimensions": {
                "actual_context_correctness": _dimension("actual_context_correctness", 1, requires=("actual_context",), description="Answer is correct when judged only against the actual retrieved context."),
                "gold_evidence_correctness": _dimension("gold_evidence_correctness", 1, requires=("gold_evidence",), description="Answer is correct when judged against the frozen Gold evidence oracle."),
                "faithfulness": _dimension("faithfulness", 1, requires=("actual_context",), description="Every material answer claim is supported by the actual retrieved context."),
                "strict_refusal": _dimension("strict_refusal", 1, requires=("actual_context",), description="For a null query, the answer refuses or states insufficiency without inventing an answer; this dimension is reported only on the null-query slice."),
            },
            "slices": ["question_type"],
            "embedding": {"required": False, "provider": EMBEDDING_PROVIDER, "model": EMBEDDING_MODEL, "only_if_exact_metric": True},
        },
        "enterprise-rag-bench": {
            "dataset": "EnterpriseRAG-Bench",
            "protocol_tag": "ENTERPRISE_CORRECTNESS_COMPLETENESS_REFUSAL",
            "implementation": "correction-aware correctness, completeness, and strict refusal",
            "dimensions": {
                "correctness": _dimension("correctness", 1, description="Answer facts agree with the frozen Enterprise Gold."),
                "completeness": _dimension("completeness", 1, requires=("gold_evidence",), description="Required Gold facts are covered without material omissions."),
                "strict_refusal": _dimension("strict_refusal", 1, description="An unanswerable item receives an evidence-grounded refusal without guessing."),
            },
            "slices": ["question_type", "source_type"],
            "embedding": {"required": False, "provider": EMBEDDING_PROVIDER, "model": EMBEDDING_MODEL, "only_if_exact_metric": True},
        },
        "fab-bench": {
            "dataset": "FAB-Bench",
            "protocol_tag": "FAB_SIX_DIMENSION_G_EVAL_ADAPTED",
            "implementation": "six fixed 0-10 domain judge dimensions at one frozen context operating point",
            "dimensions": {
                "completeness": _dimension("completeness", 10, requires=("gold_evidence",), description="All important requested technical content is covered."),
                "technical_depth": _dimension("technical_depth", 10, description="Technical explanation has appropriate depth and specificity."),
                "factuality": _dimension("factuality", 10, requires=("actual_context",), description="Technical claims are factually supported."),
                "relevance": _dimension("relevance", 10, description="The response stays focused on the question."),
                "context_utilization": _dimension("context_utilization", 10, requires=("actual_context",), description="The answer actually uses the supplied context."),
                "support_quality": _dimension("support_quality", 10, requires=("actual_context",), description="Evidence support is specific and sufficient."),
            },
            "slices": ["question_type", "context_operating_point"],
            "context_operating_point": "read from the completed run/package; never selected from scores",
            "embedding": {"required": False, "provider": EMBEDDING_PROVIDER, "model": EMBEDDING_MODEL, "only_if_exact_metric": True},
        },
        MIXED_DATASET_ID: {
            "dataset": "MOI RAG Benchmark v0.1",
            "protocol_tag": "ADAPTED_REFERENCE_RUBRIC",
            "implementation": "pure-text MaaS judge with source/question-type/answerability frozen routing; canonical claim gates are N/A when Gold lacks structured claims",
            "dimensions": {
                name: _dimension(
                    name,
                    1,
                    requires=(
                        ("actual_context",)
                        if name == "runtime_context_faithfulness"
                        else ("gold_evidence",)
                        if name == "gold_evidence_support"
                        else ("structured_reference_claims",)
                        if name == "critical_claim_coverage"
                        else ()
                    ),
                    description=(
                        "Frozen response-claim correctness label aggregate."
                        if name == "response_claim_correctness"
                        else "Reference-answer claim coverage diagnostic; canonical only when structured Gold claims exist."
                        if name == "reference_claim_recall"
                        else "Critical required claim gate; N/A without structured Gold critical claims."
                        if name == "critical_claim_coverage"
                        else "Response factual claims supported by frozen Gold evidence."
                        if name == "gold_evidence_support"
                        else "Faithfulness to explicit runtime retrieval trace only."
                        if name == "runtime_context_faithfulness"
                        else "Strict refusal success on frozen unanswerable items."
                        if name == "strict_unanswerable"
                        else "Absence of false refusal on answerable items."
                        if name == "false_refusal"
                        else "Directly answers the user question."
                        if name == "answer_relevance"
                        else "Follows frozen question/task instructions."
                        if name == "instruction_compliance"
                        else "No material contradiction in the response."
                        if name == "contradiction_free"
                        else "Unsupported response-claim diagnostic; lower raw rate is better, score is one minus rate."
                    ),
                )
                for name in MIXED_DIMENSION_NAMES
            },
            "rubric_version": MIXED_RUBRIC_VERSION,
            "rubric_by": ["source_dataset", "question_type", "answerability"],
            "rubric_mode": "ADAPTED_REFERENCE_RUBRIC",
            "rubric_routes": MIXED_RUBRICS,
            "slices": ["source_dataset", "question_type", "answerability"],
            "embedding": {"required": False, "provider": EMBEDDING_PROVIDER, "model": EMBEDDING_MODEL, "only_if_exact_metric": True},
            "canonical_claim_gate": "N/A when scored_reference_claims, critical_required_claims, or evidence_sets are absent",
        },
    }


DATASET_CONTRACTS = _contracts()
for _contract in DATASET_CONTRACTS.values():
    _contract.setdefault("primary", list(_contract["dimensions"]))
    _contract.setdefault("secondary", [])
    _contract.setdefault("denominator", "all planned initial judge units; failures remain in the denominator")
    _contract.setdefault("valid_denominator_policy", "record planned_n, eligible_n, failed_n, unsupported_n for every dimension")
    _contract.setdefault("judge_dimensions", list(_contract["dimensions"]))
DATASET_CONTRACTS["wikieval"]["secondary"] = ["source_recall", "mrr", "latency"]
DATASET_CONTRACTS["mmdocir"]["secondary"] = ["page_recall", "layout_recall", "latency"]
DATASET_CONTRACTS["mmdocrag"]["judge_dimensions"] = [
    "fluency",
    "citation_quality",
    "text_image_coherence",
    "reasoning_logic",
    "factuality",
]
DATASET_CONTRACTS["mmdocrag"]["quote_support_dimension"] = "quote_support"
DATASET_CONTRACTS["docbench"]["secondary"] = ["correctness_by_question_type", "accepted_file_rate", "searchable_ready_rate", "unanswerable_detection_rate"]
DATASET_CONTRACTS["multihop-rag"]["secondary"] = ["actual_context_correctness_by_type", "gold_evidence_correctness_by_type"]
DATASET_CONTRACTS["enterprise-rag-bench"]["secondary"] = ["strict_unanswerable_success", "conflict_resolution_accuracy"]
DATASET_CONTRACTS["fab-bench"]["secondary"] = ["overall", "context_operating_point"]


def metric_contract_for(dataset: str, *, exact_ragas: bool = False, context_operating_point: Any = None) -> dict[str, Any]:
    """Return a JSON-safe frozen metric contract for a dataset."""

    dataset_id = _norm_dataset(dataset)
    if dataset_id in EXCLUDED_DATASETS:
        raise PackageError(f"DATASET_EXCLUDED:{dataset}")
    if dataset_id not in DATASET_CONTRACTS:
        raise PackageError(f"DATASET_UNSUPPORTED:{dataset}")
    result = json.loads(json.dumps(DATASET_CONTRACTS[dataset_id], ensure_ascii=False))
    if dataset_id == "wikieval":
        if exact_ragas:
            if not ragas_0215_installed():
                raise PackageError("RAGAS_EXACT_0.2.15_NOT_INSTALLED")
            # The module intentionally does not silently substitute the
            # compatibility judge for an exact Ragas run.
            raise PackageError("RAGAS_EXACT_0.2.15_EXECUTOR_NOT_ENABLED")
        result["protocol_tag"] = RAGAS_COMPATIBLE_JUDGE
    if context_operating_point is not None:
        result["context_operating_point"] = context_operating_point
    return result


def ragas_0215_installed() -> bool:
    try:
        return importlib.metadata.version("ragas") == "0.2.15"
    except importlib.metadata.PackageNotFoundError:
        return False


def judge_response_schema(dataset: str) -> dict[str, Any]:
    contract = metric_contract_for(dataset)
    dimensions: dict[str, Any] = {}
    required = []
    for name, spec in contract["dimensions"].items():
        maximum = _scale_max(spec)
        required.append(name)
        dimensions[name] = {
            "type": "object",
            "required": ["score", "supported", "reason"],
            "additionalProperties": False,
            "properties": {
                "score": {"type": ["number", "null"], "minimum": 0, "maximum": maximum},
                "supported": {"type": "boolean"},
                "reason": {"type": "string", "minLength": 1},
            },
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"{contract['dataset']} judge response",
        "type": "object",
        "additionalProperties": False,
        "required": ["schema", "question_id", "dimensions", "overall"],
        "properties": {
            "schema": {"const": RESPONSE_SCHEMA},
            "question_id": {"type": "string", "minLength": 1},
            "dimensions": {"type": "object", "additionalProperties": False, "required": required, "properties": dimensions},
            "overall": {"type": ["number", "null"], "minimum": 0, "maximum": max(_scale_max(spec) for spec in contract["dimensions"].values())},
            "response_claims": {"type": "array", "items": {"type": "object"}},
            "adaptation": {"type": "object"},
        },
    }


def _schema_type_ok(value: Any, expected: Any) -> bool:
    expected_types = expected if isinstance(expected, list) else [expected]
    for item in expected_types:
        if item == "null" and value is None:
            return True
        if item == "object" and isinstance(value, Mapping):
            return True
        if item == "array" and isinstance(value, list):
            return True
        if item == "string" and isinstance(value, str):
            return True
        if item == "boolean" and isinstance(value, bool):
            return True
        if item == "number" and _finite_number(value):
            return True
        if item == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
    return False


def _validate_json_schema(value: Any, schema: Mapping[str, Any], path: str = "$", *, strict: bool = True) -> list[str]:
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{path}:const")
        return errors
    if "type" in schema and not _schema_type_ok(value, schema["type"]):
        errors.append(f"{path}:type")
        return errors
    if isinstance(value, Mapping):
        required = schema.get("required", [])
        for name in required:
            if name not in value:
                errors.append(f"{path}.{name}:required")
        properties = schema.get("properties", {})
        if strict and schema.get("additionalProperties") is False:
            errors.extend(f"{path}.{name}:additional" for name in value if name not in properties)
        for name, child_schema in properties.items():
            if name in value and isinstance(child_schema, Mapping):
                errors.extend(_validate_json_schema(value[name], child_schema, f"{path}.{name}", strict=strict))
    if isinstance(value, list) and isinstance(schema.get("items"), Mapping):
        for index, item in enumerate(value):
            errors.extend(_validate_json_schema(item, schema["items"], f"{path}[{index}]", strict=strict))
    if _finite_number(value):
        if schema.get("minimum") is not None and float(value) < float(schema["minimum"]):
            errors.append(f"{path}:minimum")
        if schema.get("maximum") is not None and float(value) > float(schema["maximum"]):
            errors.append(f"{path}:maximum")
    if isinstance(value, str) and schema.get("minLength") is not None and len(value) < int(schema["minLength"]):
        errors.append(f"{path}:minLength")
    return errors


def validate_judge_response(value: Any, dataset: str, question_id: str) -> dict[str, Any]:
    schema = judge_response_schema(dataset)
    errors = _validate_json_schema(value, schema)
    if errors:
        raise SchemaError("JUDGE_SCHEMA_INVALID:" + ",".join(errors[:12]))
    if str(value.get("question_id")) != str(question_id):
        raise SchemaError("JUDGE_SCHEMA_INVALID:question_id_mismatch")
    contract = metric_contract_for(dataset)
    for name, spec in contract["dimensions"].items():
        item = value["dimensions"][name]
        if item["supported"] and item["score"] is None:
            raise SchemaError(f"JUDGE_SCHEMA_INVALID:{name}:supported_score_missing")
        if not item["supported"] and item["score"] is not None:
            raise SchemaError(f"JUDGE_SCHEMA_INVALID:{name}:unsupported_score_must_be_null")
        if item["supported"] and float(item["score"]) > _scale_max(spec):
            raise SchemaError(f"JUDGE_SCHEMA_INVALID:{name}:score_out_of_range")
    return dict(value)


@dataclass
class ConditionPackage:
    root: Path
    manifest_path: Path
    manifest: dict[str, Any]
    condition_manifest: dict[str, Any]
    dataset_id: str
    dataset: str
    revision: str
    split: str
    condition: str
    questions: list[dict[str, Any]]
    corpus: list[dict[str, Any]]
    gold: list[dict[str, Any]]
    hashes: dict[str, str]

    @property
    def question_map(self) -> dict[str, dict[str, Any]]:
        return {str(row.get("question_id")): row for row in self.questions}


def _manifest_path(path: Path) -> tuple[Path, Path]:
    supplied = path.expanduser().resolve()
    if supplied.is_file():
        return supplied.parent, supplied
    if not supplied.is_dir():
        raise PackageError(f"PACKAGE_NOT_FOUND:{supplied}")
    for name in ("package.json", "manifest.json", "competitor-eval-ready.json", "package-manifest.json"):
        candidate = supplied / name
        if candidate.is_file():
            return supplied, candidate
    raise PackageError(f"PACKAGE_MANIFEST_MISSING:{supplied}")


def _load_json_or_jsonl(path: Path) -> Any:
    if path.suffix.casefold() == ".jsonl":
        return _read_jsonl(path)
    return _read_json(path)


def _resolve_package_path(root: Path, value: Any) -> Path:
    candidate = Path(str(value)).expanduser()
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def _records_from_spec(root: Path, value: Any, label: str) -> tuple[list[dict[str, Any]], Path | None]:
    source: Path | None = None
    if isinstance(value, Mapping):
        if isinstance(value.get("items"), list):
            value = value["items"]
        elif isinstance(value.get("records"), list):
            value = value["records"]
        elif value.get("path") is not None:
            value = value["path"]
    if isinstance(value, (str, Path)):
        source = _resolve_package_path(root, value)
        if not source.exists():
            raise PackageError(f"PACKAGE_{label.upper()}_MISSING:{source}")
        value = _load_json_or_jsonl(source)
    if isinstance(value, Mapping):
        for key in ("items", "records", label, "data"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
    if not isinstance(value, list):
        raise PackageError(f"PACKAGE_{label.upper()}_MUST_BE_LIST")
    result = [dict(item) for item in value if isinstance(item, Mapping)]
    if len(result) != len(value):
        raise PackageError(f"PACKAGE_{label.upper()}_ROW_NOT_OBJECT")
    return result, source


def _spec_from_manifest(manifest: Mapping[str, Any], names: Sequence[str], default: str | None = None) -> Any:
    for name in names:
        if manifest.get(name) is not None:
            return manifest[name]
    paths = manifest.get("paths")
    if isinstance(paths, Mapping):
        for name in names:
            if paths.get(name) is not None:
                return paths[name]
    artifacts = manifest.get("artifacts")
    if isinstance(artifacts, Mapping):
        for name in names:
            value = artifacts.get(name)
            if value is None and not name.endswith(".jsonl"):
                value = artifacts.get(f"{name}.jsonl")
            if value is not None:
                return value
    return default


def _validate_declared_counts(
    manifest: Mapping[str, Any],
    actual: Mapping[str, int],
    *,
    error_prefix: str = "PACKAGE",
) -> None:
    """Treat declared package counts as an admission check when present."""

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


def _validate_text_only_condition(
    manifest: Mapping[str, Any],
    condition_manifest: Mapping[str, Any],
    *,
    questions: Sequence[Mapping[str, Any]],
    corpus: Sequence[Mapping[str, Any]],
) -> None:
    """Reject vision/media inputs from the pure-text Judge path."""

    def value(name: str, default: Any = None) -> Any:
        if condition_manifest.get(name) is not None:
            return condition_manifest[name]
        return manifest.get(name, default)

    if value("mllm_required") is not False:
        raise PackageError("TEXT_ONLY_MLLM_REQUIRED_MUST_BE_FALSE")
    if str(value("image_llm", "")).strip().upper() != "NOT_APPLICABLE":
        raise PackageError("TEXT_ONLY_IMAGE_LLM_MUST_BE_NOT_APPLICABLE")

    for row in corpus:
        explicit_media = [
            row.get("media"),
            row.get("modality"),
            row.get("media_type"),
            row.get("mime_type"),
        ]
        media_text = " ".join(str(item).casefold() for item in explicit_media if item not in (None, ""))
        if "image" in media_text or "audio" in media_text or "video" in media_text:
            raise PackageError("TEXT_ONLY_CORPUS_MEDIA_UNSUPPORTED")

    for row in questions:
        image_values = row.get("images", row.get("image_paths", row.get("image_path")))
        if any(str(item).strip() for item in _as_list(image_values) if item not in (None, "")):
            raise PackageError("TEXT_ONLY_QUESTION_IMAGE_INPUT_UNSUPPORTED")
        media_text = " ".join(
            str(item).casefold()
            for item in (row.get("media"), row.get("modality"))
            if item not in (None, "")
        )
        if "image" in media_text or "audio" in media_text or "video" in media_text:
            raise PackageError("TEXT_ONLY_QUESTION_MEDIA_UNSUPPORTED")


def load_condition_package(package: str | Path, *, condition: str | None = None, exact_ragas: bool = False) -> ConditionPackage:
    root, manifest_path = _manifest_path(Path(package))
    raw = _read_json(manifest_path)
    if not isinstance(raw, Mapping):
        raise PackageError("PACKAGE_MANIFEST_MUST_BE_OBJECT")
    manifest = dict(raw.get("package", raw)) if isinstance(raw.get("package", raw), Mapping) else {}
    schema = _first(manifest, "schema", "package_schema", "schema_version", default=_first(raw, "schema", "package_schema", "schema_version"))
    if schema != PACKAGE_SCHEMA:
        raise PackageError(f"PACKAGE_SCHEMA_UNSUPPORTED:{schema}")
    dataset_value = manifest.get("dataset", manifest.get("dataset_id", ""))
    dataset = str(dataset_value.get("name") if isinstance(dataset_value, Mapping) else dataset_value)
    dataset_id = _norm_dataset(dataset)
    if dataset_id in EXCLUDED_DATASETS:
        raise PackageError(f"DATASET_EXCLUDED:{dataset}")
    if dataset_id not in DATASET_CONTRACTS:
        raise PackageError(f"DATASET_UNSUPPORTED:{dataset}")
    conditions = manifest.get("conditions")
    # Older ready-package builders used `conditions` for corpus metadata
    # (documents, question counts, selection notes), rather than a mapping of
    # named evaluation conditions.  Treat that shape as metadata so completed
    # runner records whose condition is `native` remain judgeable.
    if isinstance(conditions, Mapping):
        condition_spec_keys = {
            "questions",
            "questions_path",
            "corpus",
            "documents_path",
            "gold",
            "gold_path",
            "artifacts",
            "paths",
        }
        has_named_condition = any(
            isinstance(value, Mapping) and bool(condition_spec_keys.intersection(value))
            for value in conditions.values()
        )
        if not has_named_condition:
            conditions = None
    selected_name = str(condition or manifest.get("condition") or "")
    selected: dict[str, Any] = {}
    if isinstance(conditions, Mapping):
        normalized = {str(key).casefold(): (str(key), value) for key, value in conditions.items()}
        if selected_name and selected_name.casefold() in normalized:
            _, value = normalized[selected_name.casefold()]
            selected = dict(value) if isinstance(value, Mapping) else {}
            selected_name = normalized[selected_name.casefold()][0]
        elif len(normalized) == 1:
            selected_name, value = next(iter(normalized.values()))
            selected = dict(value) if isinstance(value, Mapping) else {}
        elif not selected_name:
            raise PackageError("PACKAGE_CONDITION_REQUIRED")
        else:
            raise PackageError(f"PACKAGE_CONDITION_UNKNOWN:{selected_name}")
    else:
        selected_name = selected_name or str(manifest.get("condition") or "native")
        selected = dict(manifest)
    condition_manifest = selected or dict(manifest)
    question_spec = _spec_from_manifest(condition_manifest, ("questions", "questions_path"))
    if question_spec is None and condition_manifest is not manifest:
        question_spec = _spec_from_manifest(manifest, ("questions", "questions_path"))
    if question_spec is None:
        question_spec = _spec_from_manifest(condition_manifest, ("questions.jsonl", "questions"), "questions.jsonl")
    corpus_spec = _spec_from_manifest(condition_manifest, ("corpus", "documents", "documents_path"))
    if corpus_spec is None and condition_manifest is not manifest:
        corpus_spec = _spec_from_manifest(manifest, ("corpus", "documents", "documents_path"))
    if corpus_spec is None:
        corpus_spec = _spec_from_manifest(condition_manifest, ("corpus.jsonl", "documents"), "corpus.jsonl")
    gold_spec = _spec_from_manifest(condition_manifest, ("gold", "gold_path"))
    if gold_spec is None and condition_manifest is not manifest:
        gold_spec = _spec_from_manifest(manifest, ("gold", "gold_path"))
    questions, question_source = _records_from_spec(root, question_spec, "questions")
    corpus, corpus_source = _records_from_spec(root, corpus_spec, "corpus")
    gold, gold_source = ([], None) if gold_spec is None else _records_from_spec(root, gold_spec, "gold")
    normalized_questions: list[dict[str, Any]] = []
    gold_by_id: dict[str, dict[str, Any]] = {}
    for ordinal, row in enumerate(gold, 1):
        gold_id = str(row.get("question_id", row.get("id", "")))
        if gold_id and gold_id in gold_by_id:
            raise PackageError(f"PACKAGE_GOLD_ID_DUPLICATE:{gold_id}")
        if gold_id:
            gold_by_id[gold_id] = row
    seen: set[str] = set()
    for ordinal, question in enumerate(questions, 1):
        qid = str(question.get("question_id", question.get("id", question.get("qid", f"question-{ordinal:04d}"))))
        if qid in seen:
            raise PackageError(f"PACKAGE_QUESTION_ID_DUPLICATE:{qid}")
        seen.add(qid)
        merged = dict(gold_by_id.get(qid, {}))
        merged.update(question)
        merged["question_id"] = qid
        merged["question"] = str(_first(merged, "question", "query", "text", default=""))
        if not merged["question"].strip():
            raise PackageError(f"PACKAGE_QUESTION_TEXT_MISSING:{qid}")
        normalized_questions.append(normalize_structured_gold(merged))
    if not normalized_questions:
        raise PackageError("PACKAGE_QUESTIONS_EMPTY")
    if not corpus:
        raise PackageError("PACKAGE_CORPUS_EMPTY")
    count_manifest = condition_manifest if isinstance(condition_manifest.get("counts"), Mapping) else manifest
    _validate_declared_counts(
        count_manifest,
        {"documents": len(corpus), "questions": len(normalized_questions), "gold": len(gold)},
    )
    if selected_name == "text-only-no-mllm":
        _validate_text_only_condition(
            manifest,
            condition_manifest,
            questions=normalized_questions,
            corpus=corpus,
        )
    revision = str(_first(condition_manifest, "revision", "dataset_revision", "package_revision", default=_first(manifest, "revision", "dataset_revision", "package_revision", "UNKNOWN")))
    split = str(_first(condition_manifest, "split", default=_first(manifest, "split", "UNKNOWN")))
    hashes: dict[str, str] = {"manifest": _hash_file(manifest_path)}
    for name, rows, source in (("questions", normalized_questions, question_source), ("corpus", corpus, corpus_source), ("gold", gold, gold_source)):
        hashes[name] = _hash_file(source) if source is not None and source.exists() else _hash_value(rows)
    contract = metric_contract_for(dataset_id, exact_ragas=exact_ragas)
    hashes["condition"] = _hash_value({"condition": selected_name, "metric_contract": contract})
    return ConditionPackage(
        root=root,
        manifest_path=manifest_path,
        manifest=manifest,
        condition_manifest=condition_manifest,
        dataset_id=dataset_id,
        dataset=contract["dataset"],
        revision=revision,
        split=split,
        condition=selected_name,
        questions=normalized_questions,
        corpus=corpus,
        gold=gold,
        hashes=hashes,
    )


@dataclass
class RunnerRun:
    root: Path
    start: dict[str, Any]
    initial: list[dict[str, Any]]
    terminal: list[dict[str, Any]]
    summary: dict[str, Any]
    hashes_verified: dict[str, bool]

    @property
    def source_run_id(self) -> str:
        return str(self.start.get("run_id") or self.summary.get("run_id") or self.root.name)

    @property
    def units(self) -> list[tuple[str, int]]:
        result: list[tuple[str, int]] = []
        for row in self.initial:
            qid = str(row.get("question_id") or _parse_attempt_id(row.get("attempt_id"))[0])
            if not qid or row.get("planned_denominator") is False:
                continue
            key = _unit_key(qid, row.get("repeat_id", _parse_attempt_id(row.get("attempt_id"))[1]))
            if key not in result:
                result.append(key)
        return result

    def _by_stage(self, stage: str) -> dict[tuple[str, int], dict[str, Any]]:
        result: dict[tuple[str, int], dict[str, Any]] = {}
        for row in self.terminal:
            if str(row.get("stage", "")).casefold() != stage.casefold():
                continue
            key = _unit_key(row.get("question_id") or _parse_attempt_id(row.get("attempt_id"))[0], row.get("repeat_id", _parse_attempt_id(row.get("attempt_id"))[1]))
            result[key] = row
        return result

    @property
    def qa_by_unit(self) -> dict[tuple[str, int], dict[str, Any]]:
        return self._by_stage("qa")

    @property
    def retrieval_by_unit(self) -> dict[tuple[str, int], dict[str, Any]]:
        return self._by_stage("retrieval")


def load_runner_run(run_dir: str | Path, *, allow_incomplete: bool = False) -> RunnerRun:
    root = Path(run_dir).expanduser().resolve()
    if not root.is_dir():
        raise RunError(f"RUN_NOT_FOUND:{root}")
    start_path = root / "start-record.json"
    initial_path = root / "initial-ledger.jsonl"
    terminal_path = root / "terminal-ledger.jsonl"
    start = _read_json(start_path)
    if not isinstance(start, Mapping):
        raise RunError("RUN_START_RECORD_NOT_OBJECT")
    initial = _read_jsonl(initial_path)
    terminal = _read_jsonl(terminal_path)
    summary_value = _read_json(root / "summary.json") if (root / "summary.json").exists() else {}
    summary = dict(summary_value) if isinstance(summary_value, Mapping) else {}
    if not allow_incomplete and str(summary.get("status", "")).upper() in {"DRY_RUN", "ERROR"}:
        raise RunError(f"RUN_NOT_COMPLETED:{summary.get('status')}")
    if not initial:
        raise RunError("RUN_INITIAL_LEDGER_EMPTY")
    if not allow_incomplete and not any(row.get("stage") == "qa" for row in terminal):
        # An explicit QA failure row is still a completed input.  Requiring a
        # QA stage prevents accidentally judging a retrieval-only run.
        raise RunError("RUN_QA_TERMINAL_LEDGER_MISSING")
    return RunnerRun(
        root=root,
        start=dict(start),
        initial=initial,
        terminal=terminal,
        summary=summary,
        hashes_verified={
            "start": _verify_sidecar(start_path),
            "initial": _verify_sidecar(initial_path),
            "terminal": _verify_sidecar(terminal_path),
        },
    )


def _provider_strings(value: Any, *, key: str = "") -> Iterable[str]:
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            lowered = str(child_key).casefold()
            if any(token in lowered for token in ("provider", "model", "endpoint", "base_url", "base-url", "engine", "generator", "judge", "embedding", "llm", "egress")):
                if isinstance(child, (str, int, float)):
                    yield str(child)
            yield from _provider_strings(child, key=str(child_key))
    elif isinstance(value, list):
        for child in value:
            yield from _provider_strings(child, key=key)
    elif key and any(token in key.casefold() for token in ("provider", "model", "endpoint", "base_url", "engine", "generator", "judge", "embedding", "llm", "egress")):
        if isinstance(value, (str, int, float)):
            yield str(value)


def _taas_values(*values: Any) -> list[str]:
    found: list[str] = []
    for value in values:
        for candidate in _provider_strings(value):
            lowered = candidate.casefold()
            if any(marker in lowered for marker in TAAS_MARKERS):
                found.append(candidate)
    return found


def _path_value(value: Any) -> str | None:
    if isinstance(value, (str, Path)):
        return str(value)
    if isinstance(value, Mapping):
        for key in ("path", "image_path", "binary_path", "asset_path", "file_path", "local_path", "filename", "file"):
            if value.get(key) not in (None, ""):
                return str(value[key])
    return None


def _resolve_local_asset(value: Any, *, run_root: Path, package_root: Path) -> Path | None:
    raw = _path_value(value)
    if not raw or _is_url(raw) or raw.startswith("data:"):
        return None
    candidate = Path(raw).expanduser()
    candidates = [candidate] if candidate.is_absolute() else [run_root / candidate, package_root / candidate, package_root.parent / candidate]
    for path in candidates:
        resolved = path.resolve()
        if resolved.is_file():
            return resolved
    return None


def _iter_evidence_items(value: Any) -> Iterable[Any]:
    if isinstance(value, list):
        yield from value
    elif isinstance(value, Mapping):
        if any(key in value for key in ("image_path", "image_paths", "binary_path", "asset_path", "image", "media_type", "mime_type")):
            yield value
        else:
            for child in value.values():
                yield from _iter_evidence_items(child)


def _actual_context(retrieval: Mapping[str, Any] | None, qa: Mapping[str, Any] | None, *, run_root: Path, package_root: Path) -> tuple[Any, list[Path]]:
    # These fields are explicit trace fields.  We intentionally do not use an
    # answer, a Gold span, a question image, or a generic ``source`` field as
    # context.
    value: Any = None
    for row in (qa, retrieval):
        if not isinstance(row, Mapping):
            continue
        for key in ("actual_context", "retrieval_context", "retrieved_contexts", "contexts", "hits", "retrieval_hits", "retrieved_evidence"):
            if key in row and row[key] not in (None, [], ""):
                value = row[key]
                break
        if value is not None:
            break
    if value is None:
        return None, []
    image_paths: list[Path] = []
    for item in _iter_evidence_items(value):
        for key in ("image_path", "image_paths", "binary_path", "asset_path", "file_path", "local_path", "image"):
            raw = item.get(key) if isinstance(item, Mapping) else None
            for candidate in _as_list(raw):
                path = _resolve_local_asset(candidate, run_root=run_root, package_root=package_root)
                if path is not None and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
                    if path not in image_paths:
                        image_paths.append(path)
        if isinstance(item, Mapping) and str(item.get("media_type", item.get("mime_type", ""))).casefold().startswith("image"):
            path = _resolve_local_asset(item.get("path"), run_root=run_root, package_root=package_root)
            if path is not None and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"} and path not in image_paths:
                image_paths.append(path)
    return value, image_paths


def has_genuine_image_evidence(value: Any, *, run_root: str | Path, package_root: str | Path) -> bool:
    """Return true only when an explicit local image asset is readable.

    A MIME label, an ``[IMAGE AVAILABLE]`` marker, a remote URL, or a
    question-level image does not satisfy this predicate.  Callers that need
    the actual assets should use :func:`_actual_context` through the normal
    :class:`JudgeRunner` path.
    """

    for item in _iter_evidence_items(value):
        for key in ("image_path", "image_paths", "binary_path", "asset_path", "file_path", "local_path", "image"):
            raw = item.get(key) if isinstance(item, Mapping) else None
            for candidate in _as_list(raw):
                path = _resolve_local_asset(candidate, run_root=Path(run_root), package_root=Path(package_root))
                if path is not None and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
                    return True
        if isinstance(item, Mapping) and str(item.get("media_type", item.get("mime_type", ""))).casefold().startswith("image"):
            path = _resolve_local_asset(item.get("path"), run_root=Path(run_root), package_root=Path(package_root))
            if path is not None and path.suffix.casefold() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
                return True
    return False


def _explicit_citations(qa: Mapping[str, Any] | None) -> Any:
    if not isinstance(qa, Mapping):
        return None
    for key in ("citations", "citation", "references", "source_citations", "answer_citations"):
        if key in qa and qa[key] not in (None, [], ""):
            return qa[key]
    return None


def _answer_from_row(row: Mapping[str, Any] | None) -> str:
    if not isinstance(row, Mapping):
        return ""
    for key in ("answer", "generated_answer", "prediction", "response", "output"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, Mapping):
            nested = _content_from_chat_response(value)
            if isinstance(nested, str) and nested.strip():
                return nested
    payload = row.get("payload")
    if isinstance(payload, Mapping):
        nested = _content_from_chat_response(payload)
        if isinstance(nested, str):
            return nested
    return ""


def _question_type(question: Mapping[str, Any]) -> str:
    return str(_first(question, "question_type", "type", "category", default="unknown"))


def _source_dataset(question: Mapping[str, Any]) -> str:
    raw = str(_first(question, "source_dataset", "source", "dataset", default="unknown") or "")
    normalized = raw.strip().casefold().replace("_", "-").replace(" ", "-")
    return MIXED_SOURCE_ALIASES.get(normalized, normalized or "unknown")


def _answerability(question: Mapping[str, Any]) -> str:
    raw = _first(question, "answerability", "answerable", default=True)
    normalized = str(raw).strip().casefold().replace("_", "-").replace(" ", "-")
    if normalized in {"false", "no", "negative", "unanswerable", "not-answerable"}:
        return "unanswerable"
    if normalized in {"unknown", "unclear", "indeterminate"}:
        return "unknown"
    return "answerable"


def _structured_reference_claims(question: Mapping[str, Any]) -> list[Any]:
    normalized = normalize_structured_gold(question)
    value = normalized.get("scored_reference_claims")
    return list(value) if isinstance(value, list) and value else []


def _structured_critical_claims(question: Mapping[str, Any]) -> list[Any]:
    normalized = normalize_structured_gold(question)
    value = normalized.get("critical_claims")
    return list(value) if isinstance(value, list) and value else []


def _structured_evidence_sets(question: Mapping[str, Any]) -> list[Any]:
    value = normalize_structured_gold(question).get("evidence_sets")
    return list(value) if isinstance(value, list) and value else []


def _canonical_claim_gold_available(question: Mapping[str, Any]) -> bool:
    return structured_gold_complete(question)


def select_frozen_rubric(question: Mapping[str, Any]) -> dict[str, Any]:
    """Select the versioned mixed-benchmark rubric without inventing Gold claims."""

    question = normalize_structured_gold(question)
    source = _source_dataset(question)
    question_type = _question_type(question).strip() or "unknown"
    answerability = _answerability(question)
    source_spec = MIXED_RUBRICS.get(source, {})
    type_spec = source_spec.get("question_types", {}) if isinstance(source_spec, Mapping) else {}
    type_key = question_type.casefold().replace("_", "-").replace(" ", "-")
    instruction = type_spec.get(type_key) or source_spec.get(answerability) or (
        "Use only the frozen question, reference answer, Gold evidence, and explicit runtime trace."
    )
    mode = "CANONICAL_CLAIM_RUBRIC" if _canonical_claim_gold_available(question) else "ADAPTED_REFERENCE_RUBRIC"
    return {
        "rubric_version": MIXED_RUBRIC_VERSION,
        "rubric_id": f"{MIXED_RUBRIC_VERSION}:{source}:{question_type}:{answerability}",
        "source_dataset": source,
        "question_type": question_type,
        "answerability": answerability,
        "mode": mode,
        "instruction": instruction,
        "canonical_claims_available": _canonical_claim_gold_available(question),
        "reference_claims_available": bool(_structured_reference_claims(question)),
        "critical_claims_available": bool(_structured_critical_claims(question)),
        "evidence_sets_available": bool(_structured_evidence_sets(question)),
    }


frozen_rubric_for = select_frozen_rubric


def _gold_evidence(question: Mapping[str, Any]) -> Any:
    for key in ("gold_evidence", "evidence", "gold_quotes", "quotes", "reference_evidence"):
        if key in question and question[key] not in (None, [], ""):
            return question[key]
    return None


def _gold_answer(question: Mapping[str, Any]) -> str:
    return str(_first(question, "reference_answer", "answer", "gold_answer", "expected_answer", default="") or "")


@dataclass
class JudgeUnit:
    question_id: str
    repeat_id: int
    question: dict[str, Any]
    qa: dict[str, Any] | None
    retrieval: dict[str, Any] | None
    answer: str
    actual_context: Any
    gold_evidence: Any
    citations: Any
    image_paths: list[Path]
    runner_status: str
    runner_error: str | None

    @property
    def context_available(self) -> bool:
        return self.actual_context not in (None, [], "")

    @property
    def gold_evidence_available(self) -> bool:
        return self.gold_evidence not in (None, [], "")

    @property
    def citations_available(self) -> bool:
        return self.citations not in (None, [], "")

    @property
    def image_evidence_available(self) -> bool:
        return bool(self.image_paths)


def _build_units(package: ConditionPackage, runner: RunnerRun) -> list[JudgeUnit]:
    questions = package.question_map
    qa_by_unit = runner.qa_by_unit
    retrieval_by_unit = runner.retrieval_by_unit
    units: list[JudgeUnit] = []
    for qid, repeat_id in runner.units:
        question = questions.get(qid)
        if question is None:
            question = {"question_id": qid, "question": "", "reference_answer": ""}
        qa = qa_by_unit.get((qid, repeat_id))
        retrieval = retrieval_by_unit.get((qid, repeat_id))
        runner_status = _status(qa.get("status") if qa else "FAILED")
        answer = _answer_from_row(qa)
        actual_context, image_paths = _actual_context(retrieval, qa, run_root=runner.root, package_root=package.root)
        units.append(
            JudgeUnit(
                question_id=qid,
                repeat_id=repeat_id,
                question=question,
                qa=qa,
                retrieval=retrieval,
                answer=answer,
                actual_context=actual_context,
                gold_evidence=_gold_evidence(question),
                citations=_explicit_citations(qa),
                image_paths=image_paths,
                runner_status=runner_status,
                runner_error=str(qa.get("error")) if qa and qa.get("error") else ("QA_TERMINAL_ROW_MISSING" if qa is None else None),
            )
        )
    return units


class JudgeStore:
    """Durable judge artifacts with an append-only terminal checkpoint."""

    def __init__(self, output: Path, *, secrets: Iterable[str] = ()) -> None:
        self.output = output.expanduser().resolve()
        self.secrets = tuple(str(value) for value in secrets if str(value))
        self.start_path = self.output / "judge-start-record.json"
        self.initial_path = self.output / "judge-initial-ledger.jsonl"
        self.terminal_path = self.output / "judge-terminal-ledger.jsonl"
        self.raw_dir = self.output / "judge-raw"
        self._lock = threading.Lock()

    def prepare(self, start_record: Mapping[str, Any], units: Sequence[JudgeUnit], *, source_run_id: str) -> None:
        self.output.mkdir(parents=True, exist_ok=True)
        if self.start_path.exists():
            existing = _read_json(self.start_path)
            if not isinstance(existing, Mapping):
                raise JudgeError("JUDGE_START_RECORD_INVALID")
            compare_keys = ("schema", "source_run_id", "dataset", "condition", "evaluation_condition", "data_hashes", "planned", "judge", "metric_contract")
            for key in compare_keys:
                if existing.get(key) != start_record.get(key):
                    raise JudgeError(f"JUDGE_START_RECORD_MISMATCH:{key}")
            if not _verify_sidecar(self.start_path):
                _write_hash_sidecar(self.start_path)
        else:
            _write_json(self.start_path, start_record, secrets=self.secrets)
        expected_keys = {(unit.question_id, unit.repeat_id) for unit in units}
        if self.initial_path.exists():
            initial_rows = _read_jsonl(self.initial_path)
            existing_keys = {_unit_key(row.get("question_id"), row.get("repeat_id", 1)) for row in initial_rows}
            if existing_keys != expected_keys or len(existing_keys) != len(initial_rows):
                raise JudgeError("JUDGE_INITIAL_LEDGER_MISMATCH")
            if not _verify_sidecar(self.initial_path):
                _write_hash_sidecar(self.initial_path)
        else:
            rows = [
                {
                    "schema": "competitor-eval-judge-initial-ledger-v1",
                    "judge_unit_id": f"{unit.question_id}#repeat-{unit.repeat_id}",
                    "source_run_id": source_run_id,
                    "question_id": unit.question_id,
                    "repeat_id": unit.repeat_id,
                    "status": "not_started",
                    "planned_denominator": True,
                    "runner_status": unit.runner_status,
                    "source_dataset": _source_dataset(unit.question),
                    "question_type": _question_type(unit.question),
                    "answerability": _answerability(unit.question),
                    "rubric": select_frozen_rubric(unit.question),
                    "context_available": unit.context_available,
                    "gold_evidence_available": unit.gold_evidence_available,
                    "citations_available": unit.citations_available,
                    "image_evidence_available": unit.image_evidence_available,
                }
                for unit in units
            ]
            _append_jsonl(self.initial_path, rows, secrets=self.secrets)
        if not self.terminal_path.exists():
            self.terminal_path.touch()
            _write_hash_sidecar(self.terminal_path)
        elif not _verify_sidecar(self.terminal_path):
            _write_hash_sidecar(self.terminal_path)

    def terminal_rows(self) -> list[dict[str, Any]]:
        return _read_jsonl(self.terminal_path, required=False)

    def terminal_keys(self) -> set[tuple[str, int]]:
        return {
            _unit_key(row.get("question_id"), row.get("repeat_id", 1))
            for row in self.terminal_rows()
            if row.get("terminal") is True or _status(row.get("status")) in TERMINAL_STATUSES
        }

    def append_terminal(self, row: Mapping[str, Any], *, recover_failed: bool = False) -> dict[str, Any]:
        key = _unit_key(row.get("question_id"), row.get("repeat_id", 1))
        with self._lock:
            existing_rows = self.terminal_rows()
            for existing in reversed(existing_rows):
                if _unit_key(existing.get("question_id"), existing.get("repeat_id", 1)) == key:
                    if not recover_failed or _status(existing.get("status")) not in FAILURE_STATUSES:
                        return existing
                    break
            value = dict(row)
            value.setdefault("terminal", True)
            value.setdefault("recorded_at", utc_now())
            if any(_unit_key(existing.get("question_id"), existing.get("repeat_id", 1)) == key for existing in existing_rows):
                value.setdefault("recovery_of_failed_terminal", True)
            _append_jsonl(self.terminal_path, [value], secrets=self.secrets)
            return value

    def write_raw(self, unit: JudgeUnit, attempt: int, *, request: Any, response: Any = None, error: Any = None) -> str:
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{_safe_slug(unit.question_id)}-repeat-{unit.repeat_id:04d}-attempt-{attempt:03d}.json"
        path = self.raw_dir / filename
        if path.exists():
            recovery = 1
            while True:
                candidate = self.raw_dir / f"{path.stem}-recovery-{recovery:03d}{path.suffix}"
                if not candidate.exists():
                    path = candidate
                    break
                recovery += 1
        _write_json(
            path,
            {
                "schema": "competitor-eval-judge-raw-v1",
                "redacted": True,
                "question_id": unit.question_id,
                "repeat_id": unit.repeat_id,
                "attempt": attempt,
                "recorded_at": utc_now(),
                "request": request,
                "response": response,
                "error": error,
            },
            secrets=self.secrets,
        )
        return path.relative_to(self.output).as_posix()


class _NoopProgress:
    def emit(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class OpenAIJudgeClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        output: Path,
        timeout: float,
        http_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.output = output
        self.timeout = timeout
        self.http_factory = http_factory
        self._client: Any = None

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        if self.http_factory is not None:
            self._client = self.http_factory(self.base_url, self.output, _NoopProgress(), timeout=self.timeout)
        elif ArtifactHTTP is not None:
            progress = Progress(self.output / "judge-progress.jsonl") if Progress is not None else _NoopProgress()
            self._client = ArtifactHTTP(self.base_url, self.output, progress, timeout=self.timeout)
        else:  # pragma: no cover - normal checkout imports ArtifactHTTP
            raise ProviderError("ARTIFACT_HTTP_UNAVAILABLE")
        return self._client

    def complete(self, body: Mapping[str, Any], *, operation: str) -> Any:
        try:
            return self._http().request(
                "POST",
                "/chat/completions",
                api_key=self.api_key,
                json_body=dict(body),
                operation=operation,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise ProviderError(f"DEEPSEEK_REQUEST_FAILED:{type(exc).__name__}:{exc}") from exc


PROMPT_TEMPLATE = """You are the frozen pure-text benchmark judge. Return only one JSON object matching the supplied JSON Schema. Use temperature 0 and the frozen rubric version. Judge only the requested dimensions. Never invent runtime context, citations, quotes, page/layout trace, Gold claims, critical claims, or document support. If a required trace or structured Gold field is unavailable, return supported=false, score=null, and an explicit N/A reason. The mixed package may use ADAPTED_REFERENCE_RUBRIC: an answer-level reference diagnostic must never be presented as canonical claim Gold. Keep scores within each dimension's declared range.\n\nDataset contract:\n{contract}\n\nInput record:\n{input_record}\n\nRequired JSON Schema:\n{schema}\n"""


def _prompt_hash(dataset: str) -> str:
    return _hash_value({"prompt_version": PROMPT_VERSION, "template": PROMPT_TEMPLATE, "dataset": metric_contract_for(dataset), "schema": judge_response_schema(dataset)})


def _judge_input(unit: JudgeUnit, package: ConditionPackage, runner: RunnerRun) -> dict[str, Any]:
    question = unit.question
    result: dict[str, Any] = {
        "dataset": package.dataset,
        "dataset_id": package.dataset_id,
        "condition": package.condition,
        "evaluation_condition": TEXT_ONLY_CONDITION if package.dataset_id == MIXED_DATASET_ID else package.condition,
        "question_id": unit.question_id,
        "source_dataset": _source_dataset(question),
        "question_type": _question_type(question),
        "question": str(question.get("question", "")),
        "reference_answer": _gold_answer(question),
        "answerability": _answerability(question),
        "citation_required": question.get("citation_required"),
        "answer": unit.answer,
        "runner_status": unit.runner_status,
        "context_available": unit.context_available,
        "gold_evidence_available": unit.gold_evidence_available,
        "citations_available": unit.citations_available,
        "image_evidence_available": unit.image_evidence_available,
        "gold_document_ids": _first(
            question,
            "gold_doc_ids",
            "gold_document_ids",
            "document_ids",
            default=[],
        ),
        "gold_evidence": unit.gold_evidence,
        "rubric": select_frozen_rubric(question),
        "reference_claim_mode": "canonical" if _canonical_claim_gold_available(question) else "adapted_reference_answer",
    }
    normalized_gold = normalize_structured_gold(question)
    for field in ("scored_reference_claims", "critical_claims", "evidence_sets"):
        if normalized_gold.get(field) not in (None, "", [], {}):
            result[field] = normalized_gold[field]
    if normalized_gold.get("structured_gold_aliases"):
        result["structured_gold_aliases"] = normalized_gold["structured_gold_aliases"]
    if unit.context_available:
        result["actual_context"] = unit.actual_context
    if unit.citations_available:
        result["citations"] = unit.citations
    # Never put local binary data in the textual prompt.  It is carried as
    # separate multimodal content only when a real local evidence asset exists.
    if unit.image_evidence_available:
        result["image_asset_count"] = len(unit.image_paths)
    return result


def _multimodal_content(text: str, image_paths: Sequence[Path]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for path in image_paths:
        mime = mimetypes.guess_type(path.name)[0] or "image/png"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
    return content


def _normalize_judgement(response: dict[str, Any], unit: JudgeUnit, dataset: str) -> dict[str, Any]:
    result = json.loads(json.dumps(response, ensure_ascii=False))
    contract = metric_contract_for(dataset)
    for name, spec in contract["dimensions"].items():
        missing: list[str] = []
        for requirement in spec.get("requires", []):
            available = {
                "actual_context": unit.context_available,
                "gold_evidence": unit.gold_evidence_available,
                "citations": unit.citations_available,
                "image_evidence": unit.image_evidence_available,
            }.get(requirement, True)
            if not available:
                missing.append(requirement)
        if missing:
            result["dimensions"][name] = {
                "score": None,
                "supported": False,
                "reason": "UNSUPPORTED_" + "_AND_".join(item.upper() for item in missing) + "_ABSENT",
            }
    if _norm_dataset(dataset) == MIXED_DATASET_ID:
        question = unit.question
        claims_available = bool(_structured_reference_claims(question))
        if not _canonical_claim_gold_available(question):
            result["dimensions"]["critical_claim_coverage"] = {
                "score": None,
                "supported": False,
                "reason": "N_A_CANONICAL_CRITICAL_GOLD_UNAVAILABLE_ADAPTED_REFERENCE_RUBRIC",
            }
        if _answerability(question) != "unanswerable":
            result["dimensions"]["strict_unanswerable"] = {
                "score": None,
                "supported": False,
                "reason": "N_A_ANSWERABLE_ITEM",
            }
        if _answerability(question) != "answerable":
            for name in MIXED_ANSWERABLE_ONLY_DIMENSION_NAMES:
                result["dimensions"][name] = {
                    "score": None,
                    "supported": False,
                    "reason": "N_A_UNANSWERABLE_ITEM",
                }
        runtime = result["dimensions"].get("runtime_context_faithfulness")
        correctness = result["dimensions"].get("response_claim_correctness")
        if (
            _answerability(question) == "answerable"
            and unit.context_available
            and isinstance(runtime, Mapping)
            and not runtime.get("supported")
            and isinstance(correctness, Mapping)
            and correctness.get("supported")
            and _finite_number(correctness.get("score"))
            and float(correctness["score"]) > 0
        ):
            result["dimensions"]["runtime_context_faithfulness"] = {
                "score": 0.0,
                "supported": True,
                "reason": "PROTOCOL_ZERO: scored answer claims are not supported by the explicit runtime context",
            }
        if not claims_available:
            # The reference-answer diagnostic is intentionally retained as an
            # adapted judgement, but its canonical critical gate remains N/A.
            result.setdefault("adaptation", {})
            result["adaptation"].update(
                {
                    "rubric": "ADAPTED_REFERENCE_RUBRIC",
                    "canonical_reference_claims": False,
                    "reference_claim_recall_is_diagnostic": True,
                    "canonical_gold_fields": ["scored_reference_claims", "critical_claims", "evidence_sets"],
                    "canonical_metrics_emitted": False,
                }
            )
    if dataset == "multihop-rag" and unit.question.get("answerable", True):
        result["dimensions"]["strict_refusal"] = {
            "score": None,
            "supported": False,
            "reason": "UNSUPPORTED_NOT_NULL_QUERY",
        }
    return result


def _repair_explicit_unsupported_scores(value: Any) -> Any:
    """Canonicalize a narrow JSON-schema contradiction from strict judges.

    Some providers occasionally return ``supported=false`` together with a
    numeric score even under strict JSON schema.  The support decision is
    unambiguous, so the only valid representation is ``score=null``.  Raw
    responses remain immutable in judge-raw; this repair neither invents a
    score nor changes a supported judgement.
    """

    if not isinstance(value, Mapping):
        return value
    result = json.loads(json.dumps(value, ensure_ascii=False))
    dimensions = result.get("dimensions")
    if isinstance(dimensions, Mapping):
        for dimension in dimensions.values():
            if isinstance(dimension, dict) and dimension.get("supported") is False:
                dimension["score"] = None
    return result


def _error_code(exc: BaseException) -> str:
    text = str(exc)
    if isinstance(exc, SchemaError) or "JUDGE_SCHEMA_INVALID" in text:
        return "JUDGE_SCHEMA_INVALID"
    if isinstance(exc, ProviderError):
        return "JUDGE_PROVIDER_FAILURE"
    return "JUDGE_FAILURE"


class JudgeRunner:
    """Load, preflight, score, resume, and aggregate one judge run."""

    def __init__(
        self,
        run_dir: str | Path,
        package: str | Path,
        output: str | Path | None = None,
        *,
        condition: str | None = None,
        env: Mapping[str, str] | None = None,
        dry_run: bool = False,
        concurrency: int = DEFAULT_CONCURRENCY,
        retries: int = DEFAULT_RETRIES,
        timeout: float = DEFAULT_TIMEOUT,
        judge_base_url: str | None = None,
        maas_base_url: str | None = None,
        http_factory: Callable[..., Any] | None = None,
        exact_ragas: bool = False,
    ) -> None:
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.package_path = Path(package).expanduser().resolve()
        self.output = (Path(output).expanduser().resolve() if output is not None else self.run_dir / "judge").resolve()
        self.condition_name = condition
        self.env = dict(os.environ if env is None else env)
        self.dry_run = bool(dry_run)
        self.concurrency = int(concurrency)
        self.retries = int(retries)
        self.timeout = float(timeout)
        # ``maas_base_url`` remains a Python API compatibility alias for old
        # callers. It no longer implies a MaaS judge and is validated against
        # the frozen official DeepSeek endpoint below.
        self.judge_base_url = str(
            judge_base_url
            or maas_base_url
            or self.env.get("DEEPSEEK_BASE_URL", DEFAULT_JUDGE_BASE_URL)
        ).rstrip("/")
        self.http_factory = http_factory
        self.exact_ragas = exact_ragas
        self.package: ConditionPackage | None = None
        self.runner: RunnerRun | None = None
        self.units: list[JudgeUnit] = []
        self.contract: dict[str, Any] | None = None
        self.store: JudgeStore | None = None

    @property
    def judge_api_key(self) -> str:
        # Deliberately read only the newly provisioned DeepSeek credential.
        # The historical key and MaaS key are not fallback candidates.
        return str(self.env.get(JUDGE_API_KEY_ENV, "")).strip()

    def _load(self) -> None:
        self.package = load_condition_package(self.package_path, condition=self.condition_name, exact_ragas=self.exact_ragas)
        self.runner = load_runner_run(self.run_dir, allow_incomplete=self.dry_run)
        self.contract = metric_contract_for(self.package.dataset_id, exact_ragas=self.exact_ragas, context_operating_point=_first(self.package.condition_manifest, "context_operating_point", "context_budget", default=_first(self.runner.start, "context_operating_point", "context_budget")))
        self.units = _build_units(self.package, self.runner)
        self.store = JudgeStore(self.output, secrets=(self.judge_api_key, self.env.get("TAAS_API_KEY", "")))

    def _taas_errors(self) -> list[str]:
        assert self.package is not None and self.runner is not None
        values = _taas_values(self.package.manifest, self.package.condition_manifest, self.runner.start, self.runner.summary)
        if any(marker in self.judge_base_url.casefold() for marker in TAAS_MARKERS):
            values.append(self.judge_base_url)
        return [f"TAAS_PROVIDER_FORBIDDEN:{value}" for value in values]

    def _frozen_provider_errors(self) -> list[str]:
        """Reject a package that explicitly asks for a different judge.

        The completed product run may have its own generator configuration;
        only an explicit condition-package judge selection is checked here.
        Missing fields are allowed for compatibility with older ready-v1
        packages and are filled by the frozen values below.
        """

        assert self.package is not None
        selection = self.package.manifest.get("provider_selection")
        if not isinstance(selection, Mapping):
            selection = self.package.condition_manifest.get("provider_selection")
        if not isinstance(selection, Mapping):
            return []
        errors: list[str] = []
        for key in ("text", "judge", "llm"):
            value = selection.get(key)
            if not isinstance(value, Mapping):
                continue
            provider = str(value.get("provider", "")).casefold()
            model = str(value.get("model", value.get("judge_model", "")))
            if provider and provider not in {JUDGE_PROVIDER, "deepseek", "dsv4f"}:
                errors.append(f"JUDGE_PROVIDER_FROZEN:{key}:{provider}")
            if model and model != TEXT_JUDGE_MODEL:
                errors.append(f"JUDGE_MODEL_FROZEN:{key}:{model}")
        if isinstance(selection.get("multimodal"), Mapping):
            errors.append("JUDGE_MULTIMODAL_FORBIDDEN")
        embedding = selection.get("embedding")
        if isinstance(embedding, Mapping):
            provider = str(embedding.get("provider", "")).casefold()
            model = str(embedding.get("model", ""))
            if provider and provider not in {"maas", "huawei-maas", "huawei_maas"}:
                errors.append(f"EMBEDDING_PROVIDER_FROZEN:{provider}")
            if model and model != EMBEDDING_MODEL:
                errors.append(f"EMBEDDING_MODEL_FROZEN:{model}")
        return errors

    def _start_record(self) -> dict[str, Any]:
        assert self.package is not None and self.runner is not None and self.contract is not None
        prompt_hash = _prompt_hash(self.package.dataset_id)
        return {
            "schema": "competitor-eval-judge-start-v1",
            "run_id": f"{self.runner.source_run_id}-judge",
            "status_at_start": "not_started",
            "source_run_id": self.runner.source_run_id,
            "source_run_dir": str(self.runner.root),
            "dataset": self.package.dataset,
            "dataset_id": self.package.dataset_id,
            "dataset_revision": self.package.revision,
            "split": self.package.split,
            "condition": self.package.condition,
            "evaluation_condition": TEXT_ONLY_CONDITION if self.package.dataset_id == MIXED_DATASET_ID else self.package.condition,
            "planned": {
                "questions": len(self.units),
                "initial_attempts": len(self.units),
                "repeats": max([unit.repeat_id for unit in self.units], default=1),
                "image_evidence_units": sum(unit.image_evidence_available for unit in self.units),
            },
            "data_hashes": {
                **self.package.hashes,
                "runner_start": _hash_value(self.runner.start),
                "runner_initial": _hash_value(self.runner.initial),
                "runner_terminal": _hash_value(self.runner.terminal),
            },
            "judge": {
                "provider": JUDGE_PROVIDER,
                "model": TEXT_JUDGE_MODEL,
                "model_version": JUDGE_MODEL_VERSION,
                "multimodal": False,
                "image_llm": "NOT_APPLICABLE",
                "temperature": 0,
                "thinking": {"type": "disabled"},
                "prompt_version": PROMPT_VERSION,
                "prompt_hash": prompt_hash,
                "response_schema": RESPONSE_SCHEMA,
                "response_schema_hash": _hash_value(judge_response_schema(self.package.dataset_id)),
                "rubric_version": self.contract.get("rubric_version"),
                "rubric_routes": sorted(select_frozen_rubric(unit.question)["rubric_id"] for unit in self.units),
                "exact_ragas": False,
            },
            "metric_contract": self.contract,
            "provider": {
                "model_egress": "external",
                "policy": "deepseek_text_maas_embedding",
                "text_judge": {"provider": JUDGE_PROVIDER, "base_url": self.judge_base_url, "model": TEXT_JUDGE_MODEL, "api_key_env": JUDGE_API_KEY_ENV, "key_loaded": bool(self.judge_api_key), "used": True},
                "taas_used": False,
                "multimodal_used": False,
                "image_llm": {"status": "NOT_APPLICABLE", "calls": 0},
            },
            "failure_policy": {
                "initial_denominator": "all planned initial judge units",
                "retry": "same judge unit; terminal row is written only after final attempt",
                "max_retries": self.retries,
                "timeout_seconds": self.timeout,
            },
            "artifact_policy": {
                "start_record": self.start_path_name,
                "initial_ledger": self.initial_path_name,
                "terminal_ledger": self.terminal_path_name,
                "raw_requests_responses": "judge-raw/*.json",
                "request_response_redacted": True,
                "hash_sidecars": True,
            },
            "runtime": {"concurrency": self.concurrency, "dry_run": self.dry_run, "host": getattr(os.uname(), "nodename", "UNKNOWN")},
        }

    @property
    def start_path_name(self) -> str:
        return "judge-start-record.json"

    @property
    def initial_path_name(self) -> str:
        return "judge-initial-ledger.jsonl"

    @property
    def terminal_path_name(self) -> str:
        return "judge-terminal-ledger.jsonl"

    def _prepare(self) -> dict[str, Any]:
        self._load()
        assert self.package is not None and self.runner is not None and self.contract is not None and self.store is not None
        errors: list[str] = []
        warnings: list[str] = []
        if self.concurrency < 1:
            errors.append("CONCURRENCY_MUST_BE_POSITIVE")
        if self.retries < 0:
            errors.append("RETRIES_MUST_BE_NON_NEGATIVE")
        if self.timeout <= 0:
            errors.append("TIMEOUT_MUST_BE_POSITIVE")
        if not _valid_judge_base_url(self.judge_base_url):
            errors.append("JUDGE_BASE_URL_MUST_USE_DEEPSEEK_OFFICIAL")
        errors.extend(self._taas_errors())
        errors.extend(self._frozen_provider_errors())
        start_dataset = _norm_dataset(self.runner.start.get("dataset", ""))
        if start_dataset and start_dataset not in {self.package.dataset_id, _norm_dataset(self.package.dataset)}:
            errors.append(f"RUN_PACKAGE_DATASET_MISMATCH:{start_dataset}:{self.package.dataset_id}")
        start_condition = str(self.runner.start.get("condition", "") or "")
        if start_condition and start_condition.casefold() != self.package.condition.casefold():
            errors.append(f"RUN_PACKAGE_CONDITION_MISMATCH:{start_condition}:{self.package.condition}")
        if not self.runner.hashes_verified["start"]:
            warnings.append("RUN_START_HASH_UNVERIFIED")
        if not self.runner.hashes_verified["initial"]:
            warnings.append("RUN_INITIAL_HASH_UNVERIFIED")
        if not self.runner.hashes_verified["terminal"]:
            warnings.append("RUN_TERMINAL_HASH_UNVERIFIED")
        dry_run_without_qa = self.dry_run and not any(row.get("stage") == "qa" for row in self.runner.terminal)
        if dry_run_without_qa:
            warnings.append("RUN_QA_TERMINAL_LEDGER_NOT_PRESENT_DRY_RUN")
        if not self.units:
            errors.append("JUDGE_INITIAL_DENOMINATOR_EMPTY")
        if not self.dry_run and not self.judge_api_key:
            errors.append(f"{JUDGE_API_KEY_ENV}_MISSING")
        # Package questions missing from the runner denominator are not
        # silently added, and runner units missing from the package remain a
        # visible failure if the run is otherwise usable.
        missing_questions = [unit.question_id for unit in self.units if not unit.question.get("question")]
        if missing_questions:
            errors.append("QUESTION_NOT_IN_PACKAGE:" + ",".join(missing_questions))
        if self.package.dataset_id == "wikieval":
            warnings.append(RAGAS_COMPATIBLE_JUDGE)
        try:
            if not errors:
                self.store.prepare(self._start_record(), self.units, source_run_id=self.runner.source_run_id)
        except JudgeError as exc:
            errors.append(str(exc))
        rubric_rows = [select_frozen_rubric(unit.question) for unit in self.units]
        return {
            "status": ("DRY_RUN" if dry_run_without_qa and not errors else ("READY" if not errors else "BLOCKED")),
            "errors": errors,
            "warnings": warnings,
            "run_id": self.runner.source_run_id,
            "dataset": self.package.dataset,
            "dataset_id": self.package.dataset_id,
            "condition": self.package.condition,
            "evaluation_condition": TEXT_ONLY_CONDITION if self.package.dataset_id == MIXED_DATASET_ID else self.package.condition,
            "planned_n": len(self.units),
            "provider": {
                "provider": JUDGE_PROVIDER,
                "model": TEXT_JUDGE_MODEL,
                "model_version": JUDGE_MODEL_VERSION,
                "thinking": {"type": "disabled"},
                "multimodal": False,
                "image_llm": "NOT_APPLICABLE",
                "api_key_env": JUDGE_API_KEY_ENV,
                "api_key_loaded": bool(self.judge_api_key),
                "base_url": self.judge_base_url,
                "taas_used": False,
                "image_calls": 0,
            },
            "rubric_version": MIXED_RUBRIC_VERSION if self.package.dataset_id == MIXED_DATASET_ID else None,
            "rubric_slices": {
                "source_dataset": sorted(Counter(row["source_dataset"] for row in rubric_rows)),
                "question_type": sorted(Counter(row["question_type"] for row in rubric_rows)),
                "answerability": sorted(Counter(row["answerability"] for row in rubric_rows)),
                "routes": sorted(row["rubric_id"] for row in rubric_rows),
            },
            "metric_contract": self.contract,
            "artifacts": {
                "start_record": str(self.output / self.start_path_name),
                "initial_ledger": str(self.output / self.initial_path_name),
                "terminal_ledger": str(self.output / self.terminal_path_name),
            },
        }

    def preflight(self) -> dict[str, Any]:
        try:
            return self._prepare()
        except (JudgeError, OSError, ValueError) as exc:
            return {"status": "BLOCKED", "errors": [str(exc)], "warnings": [], "provider": {"taas_used": False}}

    def _input_and_body(self, unit: JudgeUnit) -> tuple[dict[str, Any], dict[str, Any]]:
        assert self.package is not None and self.contract is not None
        input_record = _judge_input(unit, self.package, self.runner)  # type: ignore[arg-type]
        schema = judge_response_schema(self.package.dataset_id)
        contract_text = json.dumps(self.contract, ensure_ascii=False, sort_keys=True)
        input_text = json.dumps(input_record, ensure_ascii=False, sort_keys=True)
        schema_text = json.dumps(schema, ensure_ascii=False, sort_keys=True)
        prompt = PROMPT_TEMPLATE.format(contract=contract_text, input_record=input_text, schema=schema_text)
        model = TEXT_JUDGE_MODEL
        messages: list[dict[str, Any]] = [{"role": "system", "content": "Return only valid JSON matching the schema."}]
        messages.append({"role": "user", "content": prompt})
        try:
            max_tokens = max(512, int(self.env.get("JUDGE_MAX_TOKENS", "2048")))
        except (TypeError, ValueError):
            max_tokens = 2048
        body = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "thinking": {"type": "disabled"},
            "max_tokens": max_tokens,
            # DeepSeek dsv4f currently supports JSON object mode but rejects
            # OpenAI's json_schema response-format variant. The complete
            # schema remains in the prompt and validate_judge_response applies
            # the frozen strict contract locally before any score is accepted.
            "response_format": {"type": "json_object"},
        }
        return input_record, body

    def _execute_unit(self, unit: JudgeUnit) -> dict[str, Any]:
        assert self.package is not None and self.runner is not None and self.store is not None
        if unit.runner_status in FAILURE_STATUSES:
            return {
                "schema": "competitor-eval-judge-terminal-v1",
                "source_run_id": self.runner.source_run_id,
                "question_id": unit.question_id,
                "repeat_id": unit.repeat_id,
                "status": "FAILED",
                "error_code": "RUNNER_" + unit.runner_status,
                "error": unit.runner_error or unit.runner_status,
                "runner_status": unit.runner_status,
                "judge_model": TEXT_JUDGE_MODEL,
                "model_version": JUDGE_MODEL_VERSION,
                "image_llm": "NOT_APPLICABLE",
                "prompt_version": PROMPT_VERSION,
                "prompt_hash": _prompt_hash(self.package.dataset_id),
                "thinking": {"type": "disabled"},
                "usage": {},
                "retry_count": 0,
                "planned_denominator": True,
            }
        if unit.runner_status in UNSUPPORTED_STATUSES:
            return {
                "schema": "competitor-eval-judge-terminal-v1",
                "source_run_id": self.runner.source_run_id,
                "question_id": unit.question_id,
                "repeat_id": unit.repeat_id,
                "status": "UNSUPPORTED",
                "error_code": "RUNNER_UNSUPPORTED",
                "error": unit.runner_error or "RUNNER_UNSUPPORTED",
                "runner_status": unit.runner_status,
                "judge_model": TEXT_JUDGE_MODEL,
                "model_version": JUDGE_MODEL_VERSION,
                "image_llm": "NOT_APPLICABLE",
                "prompt_version": PROMPT_VERSION,
                "prompt_hash": _prompt_hash(self.package.dataset_id),
                "thinking": {"type": "disabled"},
                "usage": {},
                "retry_count": 0,
                "planned_denominator": True,
            }
        if unit.runner_status == "EMPTY" or not unit.answer.strip():
            return {
                "schema": "competitor-eval-judge-terminal-v1",
                "source_run_id": self.runner.source_run_id,
                "question_id": unit.question_id,
                "repeat_id": unit.repeat_id,
                "status": "FAILED",
                "error_code": "EMPTY_ANSWER",
                "runner_status": unit.runner_status,
                "judge_model": TEXT_JUDGE_MODEL,
                "model_version": JUDGE_MODEL_VERSION,
                "image_llm": "NOT_APPLICABLE",
                "prompt_version": PROMPT_VERSION,
                "prompt_hash": _prompt_hash(self.package.dataset_id),
                "thinking": {"type": "disabled"},
                "usage": {},
                "retry_count": 0,
                "planned_denominator": True,
            }
        input_record, body = self._input_and_body(unit)
        client = OpenAIJudgeClient(
            base_url=self.judge_base_url,
            api_key=self.judge_api_key,
            output=self.output,
            timeout=self.timeout,
            http_factory=self.http_factory,
        )
        raw_paths: list[str] = []
        retry_errors: list[str] = []
        for attempt in range(1, self.retries + 2):
            attempt_raw_path: str | None = None
            try:
                response = client.complete(body, operation=f"judge-{_safe_slug(unit.question_id)}-repeat-{unit.repeat_id}-attempt-{attempt}")
                raw_value = _content_from_chat_response(response)
                raw_path = self.store.write_raw(unit, attempt, request={"method": "POST", "path": "/chat/completions", "body": body, "headers": {"Authorization": "Bearer <redacted>"}}, response=response)
                raw_paths.append(raw_path)
                attempt_raw_path = raw_path
                usage = _usage_from_chat_response(response)
                raw_response_hash = _hash_value(_redacted(response, self.store.secrets))
                raw_artifact_hash = _hash_file(self.output / raw_path)
                if isinstance(raw_value, Mapping):
                    parsed = dict(raw_value)
                else:
                    parsed = json.loads(_strip_json_fence(_json_content(raw_value)))
                parsed = _repair_explicit_unsupported_scores(parsed)
                validated = validate_judge_response(parsed, self.package.dataset_id, unit.question_id)
                normalized = _normalize_judgement(validated, unit, self.package.dataset_id)
                return {
                    "schema": "competitor-eval-judge-terminal-v1",
                    "source_run_id": self.runner.source_run_id,
                    "question_id": unit.question_id,
                    "repeat_id": unit.repeat_id,
                    "status": "SUCCESS",
                    "runner_status": unit.runner_status,
                    "judge_model": body["model"],
                    "model": body["model"],
                    "model_version": JUDGE_MODEL_VERSION,
                    "provider": JUDGE_PROVIDER,
                    "prompt_version": PROMPT_VERSION,
                    "image_llm": "NOT_APPLICABLE",
                    "prompt_hash": _prompt_hash(self.package.dataset_id),
                    "response_schema_hash": _hash_value(judge_response_schema(self.package.dataset_id)),
                    "temperature": 0,
                    "thinking": {"type": "disabled"},
                    "max_tokens": body["max_tokens"],
                    "context_available": unit.context_available,
                    "gold_evidence_available": unit.gold_evidence_available,
                    "citations_available": unit.citations_available,
                    "image_evidence_available": unit.image_evidence_available,
                    "retry_count": attempt - 1,
                    "usage": usage,
                    "raw_response_hash": raw_response_hash,
                    "raw_artifact_hash": raw_artifact_hash,
                    "raw_artifact_hashes": [
                        _hash_file(self.output / path)
                        for path in raw_paths
                        if (self.output / path).is_file()
                    ],
                    "raw_redacted": True,
                    "raw_response_paths": raw_paths,
                    "retry_errors": retry_errors,
                    "judgement": normalized,
                    "planned_denominator": True,
                }
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as exc:
                code = _error_code(exc)
                retry_errors.append(f"{code}:{str(exc)}")
                try:
                    # Preserve a raw provider response when validation fails;
                    # only transport/parsing failures without a response need
                    # a second error artifact for this attempt.
                    if attempt_raw_path is None:
                        raw_path = self.store.write_raw(
                            unit,
                            attempt,
                            request={"method": "POST", "path": "/chat/completions", "body": body, "headers": {"Authorization": "Bearer <redacted>"}},
                            error={"code": code, "message": str(exc)},
                        )
                        raw_paths.append(raw_path)
                except Exception:
                    pass
                if attempt > self.retries:
                    return {
                        "schema": "competitor-eval-judge-terminal-v1",
                        "source_run_id": self.runner.source_run_id,
                        "question_id": unit.question_id,
                        "repeat_id": unit.repeat_id,
                        "status": "FAILED",
                        "runner_status": unit.runner_status,
                        "error_code": code,
                        "error": str(exc),
                        "judge_model": body["model"],
                        "model": body["model"],
                        "model_version": JUDGE_MODEL_VERSION,
                        "provider": JUDGE_PROVIDER,
                        "prompt_version": PROMPT_VERSION,
                        "image_llm": "NOT_APPLICABLE",
                        "prompt_hash": _prompt_hash(self.package.dataset_id),
                        "response_schema_hash": _hash_value(judge_response_schema(self.package.dataset_id)),
                        "temperature": 0,
                        "thinking": {"type": "disabled"},
                        "max_tokens": body["max_tokens"],
                        "context_available": unit.context_available,
                        "gold_evidence_available": unit.gold_evidence_available,
                        "citations_available": unit.citations_available,
                        "image_evidence_available": unit.image_evidence_available,
                        "retry_count": attempt - 1,
                        "usage": {},
                        "raw_artifact_hashes": [
                            _hash_file(self.output / path)
                            for path in raw_paths
                            if (self.output / path).is_file()
                        ],
                        "raw_redacted": True,
                        "raw_response_paths": raw_paths,
                        "retry_errors": retry_errors,
                        "planned_denominator": True,
                    }
                time.sleep(min(0.25 * attempt, 1.0))
        raise AssertionError("unreachable")

    def run(self) -> dict[str, Any]:
        try:
            preflight_result = self._prepare()
        except (JudgeError, OSError, ValueError) as exc:
            return {"status": "BLOCKED", "errors": [str(exc)], "warnings": [], "provider": {"taas_used": False}}
        if preflight_result["status"] != "READY":
            return preflight_result
        assert self.store is not None and self.runner is not None and self.package is not None
        if self.dry_run:
            result = {**preflight_result, "status": "DRY_RUN", "terminal_n": len(self.store.terminal_rows()), "pending_n": len(self.units) - len(self.store.terminal_keys())}
            _write_json(self.output / "judge-summary.json", result, secrets=self.store.secrets)
            return result
        terminal_rows = self.store.terminal_rows()
        latest_by_key = {
            _unit_key(row.get("question_id"), row.get("repeat_id", 1)): row
            for row in terminal_rows
        }
        # A failed provider/schema attempt remains in the append-only ledger
        # but is resumable. The recovery row is appended and aggregation uses
        # the latest terminal for that judge unit.
        done = {
            key for key, row in latest_by_key.items()
            if _status(row.get("status")) not in FAILURE_STATUSES
        }
        pending = [unit for unit in self.units if (unit.question_id, unit.repeat_id) not in done]

        def execute_and_checkpoint(unit: JudgeUnit) -> dict[str, Any]:
            row = self._execute_unit(unit)
            return self.store.append_terminal(row, recover_failed=True)

        # Judge requests are network-bound and each unit owns unique raw
        # artifact names. JudgeStore and ArtifactHTTP serialize their append /
        # artifact counters, so terminal checkpointing remains race-safe.
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            list(pool.map(execute_and_checkpoint, pending))
        aggregate_result = self.aggregate()
        result = {**preflight_result, **aggregate_result}
        result["status"] = "COMPLETE" if aggregate_result.get("denominator", {}).get("pending_n", 0) == 0 else "INCOMPLETE"
        _write_json(self.output / "judge-summary.json", result, secrets=self.store.secrets)
        return result

    def aggregate(self) -> dict[str, Any]:
        assert self.store is not None or self._load() is None
        assert self.package is not None and self.runner is not None and self.contract is not None and self.store is not None
        rows = self.store.terminal_rows()
        initial_rows = _read_jsonl(self.store.initial_path, required=False)
        planned_keys = {_unit_key(row.get("question_id"), row.get("repeat_id", 1)) for row in initial_rows}
        terminal_by_key = {_unit_key(row.get("question_id"), row.get("repeat_id", 1)): row for row in rows}
        unit_by_key = {_unit_key(unit.question_id, unit.repeat_id): unit for unit in self.units}
        judgement_by_key = {
            key: _normalize_judgement(dict(row["judgement"]), unit_by_key[key], self.package.dataset_id)
            for key, row in terminal_by_key.items()
            if key in unit_by_key and isinstance(row.get("judgement"), Mapping)
        }
        mixed_canonical_gold_missing = self.package.dataset_id == MIXED_DATASET_ID and not all(
            structured_gold_complete(unit.question) for unit in self.units
        )

        def keys_for_dimension(keys: set[tuple[str, int]], name: str) -> set[tuple[str, int]]:
            if self.package.dataset_id != MIXED_DATASET_ID:
                return keys
            if name in MIXED_ANSWERABLE_ONLY_DIMENSION_NAMES:
                return {key for key in keys if _answerability(unit_by_key[key].question) == "answerable"}
            if name in MIXED_UNANSWERABLE_ONLY_DIMENSION_NAMES:
                return {key for key in keys if _answerability(unit_by_key[key].question) == "unanswerable"}
            return keys

        def denominator_for(keys: set[tuple[str, int]]) -> dict[str, int]:
            terminal_keys = keys.intersection(terminal_by_key)
            failed_n = sum(_status(terminal_by_key[key].get("status")) in FAILURE_STATUSES for key in terminal_keys)
            unsupported_n = sum(_status(terminal_by_key[key].get("status")) in UNSUPPORTED_STATUSES for key in terminal_keys)
            return {
                "planned_n": len(keys),
                "terminal_n": len(terminal_keys),
                "pending_n": len(keys - terminal_keys),
                "failed_n": failed_n,
                "unsupported_n": unsupported_n,
                "empty_n": sum(_status(terminal_by_key[key].get("status")) == "EMPTY" for key in terminal_keys),
                "valid_n": len(terminal_keys) - failed_n - unsupported_n,
            }

        def dimension_for(keys: set[tuple[str, int]], name: str, *, canonical_gate: bool = True) -> dict[str, Any]:
            planned_n = len(keys)
            if canonical_gate and mixed_canonical_gold_missing and name in MIXED_CANONICAL_DIMENSION_NAMES:
                reason = "N/A:MISSING_STRUCTURED_CLAIM_GOLD"
                return {
                    "value": None,
                    "scale": self.contract["dimensions"][name]["scale"],
                    "numerator": 0.0,
                    "denominator": planned_n,
                    "planned_n": planned_n,
                    "observed_n": 0,
                    "missing_n": planned_n,
                    "applicable_n": 0,
                    "initial_denominator": planned_n,
                    "valid_n": 0,
                    "eligible_n": 0,
                    "failed_n": 0,
                    "na_n": planned_n,
                    "n_a_n": planned_n,
                    "N-A": planned_n,
                    "pending_n": planned_n,
                    "unsupported_n": planned_n,
                    "na_reason": reason,
                    "reason": reason,
                    "na_reasons": {"MISSING_STRUCTURED_CLAIM_GOLD": planned_n},
                    "valid_denominator_policy": "canonical structured Gold and complete judge observations are required; adapted reference diagnostics are reported separately",
                }
            scores: list[float] = []
            failed_n = 0
            na_n = 0
            pending_n = 0
            na_reasons: Counter[str] = Counter()
            for key in keys:
                row = terminal_by_key.get(key)
                if row is None:
                    pending_n += 1
                    continue
                status = _status(row.get("status"))
                if status in FAILURE_STATUSES:
                    failed_n += 1
                    continue
                if status in UNSUPPORTED_STATUSES:
                    na_n += 1
                    na_reasons["RUNNER_UNSUPPORTED"] += 1
                    continue
                judgement = judgement_by_key.get(key)
                item = judgement.get("dimensions", {}).get(name) if isinstance(judgement, Mapping) else None
                if isinstance(item, Mapping) and item.get("supported") and _finite_number(item.get("score")):
                    scores.append(float(item["score"]))
                else:
                    na_n += 1
                    reason = str(item.get("reason") if isinstance(item, Mapping) else "JUDGEMENT_DIMENSION_MISSING")
                    na_reasons[reason] += 1
            numerator = sum(scores) if scores else 0.0
            metric_denominator_n = len(scores) + failed_n
            missing_n = pending_n + na_n
            value = (numerator / metric_denominator_n) if metric_denominator_n and not missing_n else None
            result = {
                "value": value,
                "scale": self.contract["dimensions"][name]["scale"],
                "numerator": numerator,
                "denominator": planned_n if missing_n else metric_denominator_n,
                "planned_n": planned_n,
                "observed_n": len(scores),
                "missing_n": missing_n,
                "applicable_n": metric_denominator_n if not missing_n else 0,
                "initial_denominator": planned_n,
                "valid_n": len(scores),
                "eligible_n": len(scores),
                "failed_n": failed_n,
                "na_n": na_n,
                "n_a_n": na_n,
                "N-A": na_n,
                "pending_n": pending_n,
                "unsupported_n": na_n,
                "na_reason": "N/A:INCOMPLETE_PLANNED_JUDGE_OBSERVATIONS" if missing_n else None,
                "reason": "INCOMPLETE_PLANNED_JUDGE_OBSERVATIONS" if missing_n else None,
                "na_reasons": dict(sorted(na_reasons.items())),
                "valid_denominator_policy": "dimension-applicable planned units; failed applicable units remain in the denominator; N-A inputs remain explicit and are not imputed",
            }
            return result

        def report_for(keys: set[tuple[str, int]]) -> dict[str, Any]:
            return {
                "denominator": denominator_for(keys),
                "dimensions": {
                    name: dimension_for(keys_for_dimension(keys, name), name)
                    for name in self.contract["dimensions"]
                },
            }

        denominator = denominator_for(planned_keys)
        dimension_names = list(self.contract["dimensions"])
        dimensions = {
            name: dimension_for(keys_for_dimension(planned_keys, name), name)
            for name in dimension_names
        }
        adapted_dimension_sources = (
            {
                "response_claim_correctness": "response_claim_correctness",
                "reference_claim_recall_adapted": "reference_claim_recall",
                "critical_claim_coverage_adapted": "critical_claim_coverage",
                "gold_evidence_support_adapted": "gold_evidence_support",
                "runtime_context_faithfulness": "runtime_context_faithfulness",
            }
            if self.package.dataset_id == MIXED_DATASET_ID
            else {}
        )
        adapted_dimensions = {}
        for output_name, source_name in adapted_dimension_sources.items():
            record = dimension_for(keys_for_dimension(planned_keys, source_name), source_name, canonical_gate=False)
            record["metric_id"] = output_name
            record["protocol_label"] = "ADAPTED_REFERENCE_RUBRIC"
            adapted_dimensions[output_name] = record

        def unique_labels(selector: Callable[[JudgeUnit], str]) -> list[str]:
            return sorted({selector(unit) for unit in self.units})

        by_question_type = {
            label: report_for({_unit_key(unit.question_id, unit.repeat_id) for unit in self.units if _question_type(unit.question) == label})
            for label in unique_labels(lambda unit: _question_type(unit.question))
        }
        by_source_dataset = {
            label: report_for({_unit_key(unit.question_id, unit.repeat_id) for unit in self.units if _source_dataset(unit.question) == label})
            for label in unique_labels(lambda unit: _source_dataset(unit.question))
        }
        by_answerability = {
            label: report_for({_unit_key(unit.question_id, unit.repeat_id) for unit in self.units if _answerability(unit.question) == label})
            for label in unique_labels(lambda unit: _answerability(unit.question))
        }
        by_source_and_type: dict[str, Any] = {}
        for source in unique_labels(lambda unit: _source_dataset(unit.question)):
            by_source_and_type[source] = {}
            source_units = [unit for unit in self.units if _source_dataset(unit.question) == source]
            for question_type in sorted({_question_type(unit.question) for unit in source_units}):
                by_source_and_type[source][question_type] = report_for(
                    {_unit_key(unit.question_id, unit.repeat_id) for unit in source_units if _question_type(unit.question) == question_type}
                )

        usage_totals: Counter[str] = Counter()
        for row in terminal_by_key.values():
            usage = row.get("usage")
            if isinstance(usage, Mapping):
                for key, value in usage.items():
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        usage_totals[str(key)] += value
        canonical_claims_complete = all(
            bool(_structured_reference_claims(unit.question))
            and bool(_structured_critical_claims(unit.question))
            and bool(_structured_evidence_sets(unit.question))
            for unit in self.units
        )
        if self.package.dataset_id == MIXED_DATASET_ID and not canonical_claims_complete:
            tdas = {
                "value": None,
                "status": "N/A",
                "mode": "ADAPTED_REFERENCE_RUBRIC",
                "reason": "MISSING_STRUCTURED_CLAIM_GOLD",
                "na_reason": "N/A:MISSING_STRUCTURED_CLAIM_GOLD",
                "planned_n": len(self.units),
                "observed_n": 0,
                "missing_n": len(self.units),
            }
        else:
            tdas = {"value": None, "status": "NOT_COMPUTED", "mode": "CANONICAL_CLAIM_RUBRIC"}
        return {
            "schema": "competitor-eval-judge-aggregate-v1",
            "status": "COMPLETE" if denominator["pending_n"] == 0 else "INCOMPLETE",
            "dataset": self.package.dataset,
            "dataset_id": self.package.dataset_id,
            "condition": self.package.condition,
            "evaluation_condition": TEXT_ONLY_CONDITION if self.package.dataset_id == MIXED_DATASET_ID else self.package.condition,
            "protocol_tag": self.contract["protocol_tag"],
            "rubric_version": self.contract.get("rubric_version"),
            "judge": {
                "provider": JUDGE_PROVIDER,
                "model": TEXT_JUDGE_MODEL,
                "model_version": JUDGE_MODEL_VERSION,
                "multimodal": False,
                "image_llm": "NOT_APPLICABLE",
                "thinking": {"type": "disabled"},
                "temperature": 0,
                "prompt_version": PROMPT_VERSION,
                "prompt_hash": _prompt_hash(self.package.dataset_id),
                "usage": dict(sorted(usage_totals.items())),
            },
            "context_operating_point": self.contract.get("context_operating_point"),
            "denominator": denominator,
            "dimensions": dimensions,
            "adapted_dimensions": adapted_dimensions,
            "by_question_type": by_question_type,
            "by_source_dataset": by_source_dataset,
            "by_answerability": by_answerability,
            "by_source_dataset_question_type": by_source_and_type,
            "tdas": tdas,
            "artifacts": {
                "terminal_ledger": str(self.store.terminal_path),
                "initial_ledger": str(self.store.initial_path),
                "raw_dir": str(self.store.raw_dir),
            },
        }


def preflight(
    run_dir: str | Path,
    package: str | Path,
    *,
    output: str | Path | None = None,
    condition: str | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    retries: int = DEFAULT_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    judge_base_url: str | None = None,
    maas_base_url: str | None = None,
    exact_ragas: bool = False,
) -> dict[str, Any]:
    return JudgeRunner(run_dir, package, output, condition=condition, env=env, dry_run=dry_run, concurrency=concurrency, retries=retries, timeout=timeout, judge_base_url=judge_base_url, maas_base_url=maas_base_url, exact_ragas=exact_ragas).preflight()


def run(
    run_dir: str | Path,
    package: str | Path,
    *,
    output: str | Path | None = None,
    condition: str | None = None,
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    concurrency: int = DEFAULT_CONCURRENCY,
    retries: int = DEFAULT_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    judge_base_url: str | None = None,
    maas_base_url: str | None = None,
    http_factory: Callable[..., Any] | None = None,
    retries_override: int | None = None,
    exact_ragas: bool = False,
) -> dict[str, Any]:
    return JudgeRunner(run_dir, package, output, condition=condition, env=env, dry_run=dry_run, concurrency=concurrency, retries=retries if retries_override is None else retries_override, timeout=timeout, judge_base_url=judge_base_url, maas_base_url=maas_base_url, http_factory=http_factory, exact_ragas=exact_ragas).run()


def aggregate(
    run_dir: str | Path,
    package: str | Path,
    *,
    output: str | Path | None = None,
    condition: str | None = None,
    env: Mapping[str, str] | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    retries: int = DEFAULT_RETRIES,
    timeout: float = DEFAULT_TIMEOUT,
    judge_base_url: str | None = None,
    maas_base_url: str | None = None,
    exact_ragas: bool = False,
) -> dict[str, Any]:
    runner = JudgeRunner(run_dir, package, output, condition=condition, env=env, concurrency=concurrency, retries=retries, timeout=timeout, judge_base_url=judge_base_url, maas_base_url=maas_base_url, exact_ragas=exact_ragas)
    try:
        runner._load()
        assert runner.store is not None and runner.package is not None and runner.runner is not None and runner.contract is not None
        if not runner.store.initial_path.exists():
            return {"status": "BLOCKED", "errors": ["JUDGE_INITIAL_LEDGER_MISSING"]}
        return runner.aggregate()
    except (JudgeError, OSError, ValueError) as exc:
        return {"status": "BLOCKED", "errors": [str(exc)]}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "run", "aggregate"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--run-dir", "--run", dest="run_dir", type=Path, required=True)
        sub.add_argument("--package", "--condition-package", dest="package", type=Path, required=True)
        sub.add_argument("--output", "--output-dir", dest="output", type=Path, default=None)
        sub.add_argument("--condition", default=None)
        sub.add_argument("--dry-run", action="store_true")
        sub.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
        sub.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
        sub.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
        sub.add_argument("--judge-base-url", "--deepseek-base-url", dest="judge_base_url", default="")
        sub.add_argument("--maas-base-url", default="", help="legacy alias; must still resolve to the official DeepSeek endpoint")
        sub.add_argument("--exact-ragas", action="store_true", help="require the exact installed ragas==0.2.15 executor; never falls back")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        options = {
            "output": args.output,
            "condition": args.condition,
            "dry_run": args.dry_run,
            "concurrency": args.concurrency,
            "retries": args.retries,
            "timeout": args.timeout,
            "judge_base_url": args.judge_base_url or None,
            "maas_base_url": args.maas_base_url or None,
            "exact_ragas": args.exact_ragas,
        }
        if args.command == "preflight":
            result = preflight(args.run_dir, args.package, **options)
        elif args.command == "run":
            result = run(args.run_dir, args.package, **options)
        else:
            result = aggregate(args.run_dir, args.package, **{key: value for key, value in options.items() if key != "dry_run"})
        print(json.dumps(redact(result), ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("status") in {"READY", "DRY_RUN", "COMPLETE", "INCOMPLETE"} else 2
    except (JudgeError, OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
