#!/usr/bin/env python3
"""Run one comparable Lenovo latency pass against the four local RAG products.

The benchmark has two deliberately separate tracks:

* direct retrieval, which is the closest available public/diagnostic contract for
  measuring the local vector-search path; and
* streaming application requests, used only for transport metrics such as TTFE
  and event throughput.

MOI uses the official Catalog A2A/SSE data plane for application metrics.  Its
native MatrixFlow CLI remains a separate direct-retrieval diagnostic and is
never exposed through an HTTP wrapper.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Callable, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[4]
PLATFORM_ROOT = ROOT / "local-rag-platforms"
DATASET_DEFAULT = ROOT / "datasets/lenovo-bench/moi-corpus-100q-v1/questions.all.jsonl"
PLATFORM_ORDER = ("moi", "dify", "fastgpt", "maxkb")
MOI_ROOT = ROOT / "moi-prototypes/local-matrixflow-rag"
MOI_LAUNCHER = MOI_ROOT / "main.go"
MOI_CONFIG = MOI_ROOT / "config.lenovo-bench.latency.maas.json"

def _configured_path(env_name: str, default: Path) -> Path:
    """Allow a benchmark pass to bind an explicit resource manifest/secret.

    The repository keeps prior benchmark runs immutable.  An environment
    override lets a later pass select a new provider-specific resource set
    without silently replacing the checked-in default paths.
    """

    value = os.environ.get(env_name, "").strip()
    return Path(value).expanduser() if value else default


DIFY_RESOURCE_MAP = _configured_path(
    "LENOVO_DIFY_RESOURCE_MAP",
    ROOT
    / "runs/dify-lenovo-bench-20260813/dify-local-lenovo-bench-formal-v1/resource-map.json",
)
DIFY_APP_KEY = _configured_path(
    "LENOVO_DIFY_APP_KEY",
    ROOT
    / "runs/dify-lenovo-bench-20260813/dify-local-lenovo-bench-formal-v1/secrets/dify_local-global-app.key",
)
FASTGPT_RESOURCE_MAP = _configured_path(
    "LENOVO_FASTGPT_RESOURCE_MAP",
    ROOT
    / "runs/stage1/lenovo-bench-fastgpt/20260812-fastgpt-lenovo-bench-native-v5-final/fastgpt_local/native/resources.json",
)
MAXKB_RESOURCE_MAP = _configured_path(
    "LENOVO_MAXKB_RESOURCE_MAP",
    ROOT
    / "runs/maxkb-lenovo-bench-20260813/maxkb-local-lenovo-bench-chunked-text-v1/resource-map.json",
)
MAXKB_APP_KEY = _configured_path(
    "LENOVO_MAXKB_APP_KEY",
    ROOT
    / "runs/maxkb-lenovo-bench-20260813/maxkb-local-lenovo-bench-chunked-text-v1/secrets/maxkb_local-global-app.key",
)
MAXKB_ADMIN_TOKEN = ROOT / ".local-services/maxkb_local/secrets/admin.token"


# The reusable HTTP/SSE implementation already has the canonical metric
# definitions.  Keep this runner thin and use the same parser/summary code.
sys.path.insert(0, str(PLATFORM_ROOT / "dify-rag-eval/src"))
from dify_rag_eval.api_benchmark import (  # noqa: E402
    ScenarioConfig,
    TargetConfig,
    _distribution,
    build_builtin_targets,
    measure_request,
    summarize_samples,
)


RequestFn = Callable[..., dict[str, Any]]


def _load_env_file(path: Path, environ: dict[str, str]) -> None:
    """Load simple KEY=VALUE files without replacing non-empty env values."""

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
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if not environ.get(key):
            environ[key] = value


def load_local_env() -> dict[str, str]:
    """Collect deployment env files while keeping explicit process env values."""

    environ = dict(os.environ)
    candidates = (ROOT / ".env",)
    seen: set[Path] = set()
    for path in candidates:
        if path not in seen:
            _load_env_file(path, environ)
            seen.add(path)
    return environ


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_secret(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _resource_global(path: Path) -> dict[str, Any]:
    """Return the ready Lenovo resource contract from a resource-map JSON."""

    raw = _read_json(path)
    resources = raw.get("resources", raw)
    if isinstance(resources, Mapping):
        value = resources.get("__global__", resources)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def enrich_resource_env(environ: dict[str, str]) -> dict[str, str]:
    """Bind the checked-in run manifests to the local API env aliases."""

    try:
        dify = _resource_global(DIFY_RESOURCE_MAP)
        dify_app = dify.get("app") or {}
        dify_dataset = dify.get("dataset") or {}
        dify_id = str(
            dify.get("dataset_id")
            or dify_dataset.get("id")
            or dify_dataset.get("dataset_id")
            or ""
        )
        if dify_id:
            environ["DIFY_DATASET_ID"] = dify_id
        app_id = str(dify.get("app_id") or dify_app.get("id") or "")
        if app_id:
            environ["DIFY_APP_ID"] = app_id
    except (OSError, ValueError, TypeError):
        pass
    dify_key = _read_secret(DIFY_APP_KEY)
    if dify_key:
        environ["DIFY_API_KEY"] = dify_key
        environ["DIFY_LOCAL_API_KEY"] = dify_key
    if environ.get("DIFY_LOCAL_DATASET_API_KEY"):
        environ["DIFY_DATASET_API_KEY"] = environ["DIFY_LOCAL_DATASET_API_KEY"]

    try:
        fastgpt = _read_json(FASTGPT_RESOURCE_MAP)
        if str(fastgpt.get("app_id") or ""):
            # This is the Lenovo v5 app, not the older generic smoke-test app.
            environ["FASTGPT_APP_ID"] = str(fastgpt["app_id"])
        if str(fastgpt.get("dataset_id") or ""):
            environ["FASTGPT_DATASET_ID"] = str(fastgpt["dataset_id"])
    except (OSError, ValueError, TypeError):
        pass

    try:
        maxkb = _resource_global(MAXKB_RESOURCE_MAP)
        knowledge = maxkb.get("knowledge") or {}
        app = maxkb.get("app") or {}
        knowledge_id = str(
            maxkb.get("knowledge_id") or knowledge.get("id") or ""
        )
        app_id = str(maxkb.get("app_id") or app.get("id") or "")
        if knowledge_id:
            environ["MAXKB_KNOWLEDGE_ID"] = knowledge_id
        if app_id:
            environ["MAXKB_APPLICATION_ID"] = app_id
    except (OSError, ValueError, TypeError):
        pass
    maxkb_key = _read_secret(MAXKB_APP_KEY)
    if maxkb_key:
        environ["MAXKB_API_KEY"] = maxkb_key
    maxkb_admin = _read_secret(MAXKB_ADMIN_TOKEN)
    if maxkb_admin:
        environ["MAXKB_ADMIN_TOKEN"] = maxkb_admin
    return environ


def load_query_rows(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        value = json.loads(raw)
        question = str(value.get("question") or value.get("query") or "").strip()
        question_id = str(
            value.get("question_id") or value.get("id") or f"q-{line_number:04d}"
        )
        if question:
            row = dict(value)
            row["question_id"] = question_id
            row["question"] = question
            rows.append(row)
    if not rows:
        raise ValueError(f"no usable questions found in {path}")
    return rows


def select_queries(
    rows: Sequence[Mapping[str, Any]], count: int, seed: int
) -> list[dict[str, Any]]:
    """Select a reproducible sample without replacement."""

    if count <= 0:
        raise ValueError("count must be greater than zero")
    if count > len(rows):
        raise ValueError(f"count={count} exceeds available questions={len(rows)}")
    return [dict(row) for row in random.Random(seed).sample(list(rows), count)]


def _replace_query_value(value: Any, question: str) -> Any:
    if isinstance(value, Mapping):
        return {key: _replace_query_value(item, question) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_query_value(item, question) for item in value]
    if isinstance(value, tuple):
        return [_replace_query_value(item, question) for item in value]
    if isinstance(value, str):
        return value.replace("{{query}}", question)
    return value


def replace_query(scenario: ScenarioConfig, question: str) -> ScenarioConfig:
    """Render the benchmark query placeholder into a scenario body."""

    return replace(scenario, body=_replace_query_value(copy.deepcopy(scenario.body), question))


def make_test_target_and_scenario() -> tuple[TargetConfig, ScenarioConfig]:
    """Small public factory used by the unit tests for the batch scheduler."""

    scenario = ScenarioConfig(
        name="events",
        path="/",
        body={"query": "{{query}}"},
        protocol="json",
    )
    target = TargetConfig(
        name="test",
        base_url="http://127.0.0.1",
        api_key_env=None,
        auth_header=None,
        event=scenario,
        empty_workflow=scenario,
    )
    return target, scenario


class _InFlight:
    def __init__(self) -> None:
        self.lock = Lock()
        self.current = 0
        self.peak = 0
        self.last_change = time.perf_counter()
        self.area = 0.0

    def _advance(self, now: float) -> None:
        self.area += self.current * (now - self.last_change)
        self.last_change = now

    def enter(self) -> None:
        with self.lock:
            self._advance(time.perf_counter())
            self.current += 1
            self.peak = max(self.peak, self.current)

    def leave(self) -> None:
        with self.lock:
            self._advance(time.perf_counter())
            self.current = max(0, self.current - 1)

    def average(self, elapsed_s: float) -> float:
        with self.lock:
            self._advance(time.perf_counter())
            return self.area / max(elapsed_s, 0.000001)


def _exception_sample(exc: BaseException, index: int, worker_id: int) -> dict[str, Any]:
    return {
        "request_index": index,
        "worker_id": worker_id,
        "success": False,
        "status_code": None,
        "events": 0,
        "first_event_ms": None,
        "total_ms": None,
        "stream_ms": 0,
        "bytes_received": 0,
        "event_type_counts": {},
        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
    }


def run_request_batch(
    target: TargetConfig,
    scenario_factory: Callable[[Mapping[str, Any]], ScenarioConfig],
    queries: Sequence[Mapping[str, Any]],
    environ: Mapping[str, str],
    connections: int,
    timeout_s: float,
    request_fn: RequestFn = measure_request,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Issue exactly one request per query with a bounded parallel pool."""

    if connections <= 0:
        raise ValueError("connections must be greater than zero")
    started = time.perf_counter()
    tracker = _InFlight()
    samples: list[dict[str, Any] | None] = [None] * len(queries)

    def one(index: int, query: Mapping[str, Any]) -> None:
        worker_id = index % connections
        tracker.enter()
        try:
            sample = request_fn(
                target,
                scenario_factory(query),
                environ,
                timeout_s,
                worker_id=worker_id,
                request_index=index,
            )
        except Exception as exc:  # a single failed product request must not abort the pass
            sample = _exception_sample(exc, index, worker_id)
        finally:
            tracker.leave()
        sample = dict(sample)
        sample["question_id"] = str(query.get("question_id") or query.get("id") or index)
        sample["query_chars"] = len(str(query.get("question") or ""))
        samples[index] = sample

    with ThreadPoolExecutor(max_workers=connections) as executor:
        futures = [executor.submit(one, index, query) for index, query in enumerate(queries)]
        for future in futures:
            future.result()
    elapsed = max(time.perf_counter() - started, 0.000001)
    complete = [sample for sample in samples if sample is not None]
    return (
        summarize_samples(
            complete, elapsed, connections, tracker.peak, tracker.average(elapsed)
        ),
        complete,
    )


def unsupported_empty_workflow(reason: str) -> dict[str, Any]:
    """Represent an intentionally unmeasured empty workflow as N/A, not zero."""

    return {
        "status": "unsupported",
        "reason": reason,
        "requests": 0,
        "successes": 0,
        "errors": 0,
        "qps": None,
        "event_throughput_events_per_s": None,
        "ttfe_ms": _distribution([]),
        "latency_ms": _distribution([]),
        "connections": 0,
        "peak_in_flight": 0,
        "average_in_flight": 0.0,
    }


def cli_first_output_latencies(output_times_ms: Sequence[float]) -> list[float]:
    """Convert cumulative CLI result-output times into per-query intervals.

    The MOI CLI emits one terminal ``attempt=...`` line after each query.  The
    first line is measured from process start; each later line is measured from
    the preceding line.  This is intentionally a CLI first-output proxy, not
    an HTTP streaming TTFE measurement.
    """

    values: list[float] = []
    previous: float | None = None
    for raw in output_times_ms:
        current = float(raw)
        if current < 0 or (previous is not None and current < previous):
            raise ValueError("CLI output timestamps must be non-negative and monotonic")
        values.append(round(current if previous is None else current - previous, 3))
        previous = current
    return values


def _skipped_summary(reason: str, connections: int) -> dict[str, Any]:
    return {
        "status": "skipped",
        "reason": reason,
        "requests": 0,
        "successes": 0,
        "errors": 0,
        "events": 0,
        "qps": None,
        "event_throughput_events_per_s": None,
        "stream_event_rate_events_per_s": None,
        "connections": connections,
        "configured_connections": connections,
        "peak_in_flight": 0,
        "average_in_flight": 0.0,
        "ttfb_ms": _distribution([]),
        "ttfe_ms": _distribution([]),
        "latency_ms": _distribution([]),
    }


def _summary_status(summary: Mapping[str, Any]) -> str:
    requests = int(summary.get("requests", 0) or 0)
    successes = int(summary.get("successes", 0) or 0)
    if requests and successes == requests:
        return "ok"
    if successes:
        return "partial"
    return "error"


def _warmup_retrieval(
    target: TargetConfig,
    query: Mapping[str, Any],
    environ: Mapping[str, str],
    timeout_s: float,
) -> dict[str, Any]:
    """Wait for a product's vector path without counting the probe in the sample."""

    if not target.event.supported:
        return {"status": "skipped", "reason": target.event.note or "not configured"}
    if target.missing_requirements(environ):
        return {"status": "skipped", "reason": "missing credential/config"}
    started = time.perf_counter()
    last: dict[str, Any] | None = None
    for attempt in range(1, 4):
        last = measure_request(
            target,
            replace_query(target.event, str(query.get("question") or "")),
            environ,
            min(timeout_s, 60.0),
            worker_id=-1,
            request_index=-1,
        )
        if last.get("success"):
            return {
                "status": "ok",
                "attempts": attempt,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
            }
        if attempt < 3:
            time.sleep(2.0)
    return {
        "status": "error",
        "attempts": 3,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "reason": str((last or {}).get("error") or "warmup request failed"),
    }


def _event_scenario() -> ScenarioConfig:
    return ScenarioConfig(
        name="events",
        path="/chat-messages",
        protocol="sse",
        body={
            "inputs": {},
            "query": "{{query}}",
            "response_mode": "streaming",
            "conversation_id": "",
            "user": "lenovo-latency-{{uuid}}",
        },
    )


def build_lenovo_api_targets(
    environ: Mapping[str, str],
) -> dict[str, dict[str, TargetConfig]]:
    """Build direct-retrieval and streaming contracts for the ready resources."""

    env = dict(environ)
    builtins = build_builtin_targets(env)

    moi_workspace_id = env.get("MOI_NATIVE_WORKSPACE_ID", "")
    moi_event = ScenarioConfig(
        name="events",
        path="/api/v1/agents/a2a" if moi_workspace_id else "",
        protocol="sse",
        supported=bool(moi_workspace_id),
        headers={"X-Workspace-ID": "${MOI_NATIVE_WORKSPACE_ID}"},
        body={
            "agent_code": "explore",
            "jsonrpc": "2.0",
            "id": "{{uuid}}",
            "method": "message/stream",
            "params": {
                "model": env.get("MOI_NATIVE_MODEL", "deepseek-chat"),
                "message": {
                    "kind": "message",
                    "role": "user",
                    "messageId": "{{uuid}}",
                    "parts": [{"kind": "text", "text": "{{query}}"}],
                },
            },
        },
        note="MOI official Catalog A2A message/stream endpoint.",
    )
    moi_app = TargetConfig(
        name="moi",
        base_url=env.get("MOI_NATIVE_BASE_URL", "http://127.0.0.1:8081"),
        api_key_env="MOI_NATIVE_API_KEY",
        auth_header="X-API-Key",
        auth_scheme="raw",
        event=moi_event,
        empty_workflow=moi_event,
        required_env=("MOI_NATIVE_WORKSPACE_ID",),
        metadata={"implementation": "MOI official Catalog A2A SSE"},
    )
    moi_retrieval = ScenarioConfig(
        name="retrieval",
        path="",
        protocol="json",
        supported=False,
        note="MOI A2A does not expose a direct-retrieval HTTP contract; native SearchRAGChunks is measured separately.",
    )
    moi_db = replace(moi_app, name="moi-retrieval", event=moi_retrieval)

    dify_base = (env.get("DIFY_API_BASE_URL") or "http://127.0.0.1:8010/v1").rstrip("/")
    dify_event = _event_scenario()
    dify_app = TargetConfig(
        name="dify",
        base_url=dify_base,
        api_key_env="DIFY_API_KEY",
        auth_header="Authorization",
        event=dify_event,
        empty_workflow=dify_event,
        metadata={"implementation": "Dify Chat Messages streaming API"},
    )
    dify_dataset_id = env.get("DIFY_DATASET_ID", "")
    dify_retrieval = ScenarioConfig(
        name="retrieval",
        path=f"/datasets/{dify_dataset_id}/retrieve" if dify_dataset_id else "",
        protocol="json",
        supported=bool(dify_dataset_id),
        note="Dify dataset direct retrieval API; reranking disabled.",
        body={
            "query": "{{query}}",
            "retrieval_model": {
                "search_method": "semantic_search",
                "reranking_enable": False,
                "top_k": 5,
                "score_threshold_enabled": False,
            },
        },
    )
    dify_db = replace(
        dify_app,
        name="dify-retrieval",
        api_key_env="DIFY_DATASET_API_KEY",
        event=dify_retrieval,
        empty_workflow=dify_retrieval,
        metadata={"implementation": "Dify dataset retrieve API"},
    )

    fast_base = (env.get("FASTGPT_BASE_URL") or "http://127.0.0.1:3000").rstrip("/")
    fast_event = replace(
        builtins["fastgpt"].event,
        name="events",
        protocol="sse",
        body={
            "appId": "${FASTGPT_APP_ID}",
            "chatId": "{{uuid}}",
            "stream": True,
            "detail": True,
            "messages": [{"role": "user", "content": "{{query}}"}],
        },
    )
    fast_app = replace(
        builtins["fastgpt"],
        base_url=fast_base,
        api_key_env="FASTGPT_API_KEY",
        event=fast_event,
        empty_workflow=fast_event,
        metadata={"implementation": "FastGPT OpenAI-compatible chat streaming API"},
    )
    fast_dataset_id = env.get("FASTGPT_DATASET_ID", "")
    fast_retrieval = ScenarioConfig(
        name="retrieval",
        path="/api/core/dataset/searchTest" if fast_dataset_id else "",
        protocol="json",
        supported=bool(fast_dataset_id),
        note="FastGPT dataset searchTest API; embedding-only mode with rerank disabled.",
        body={
            "datasetId": fast_dataset_id,
            "text": "{{query}}",
            "limit": 20000,
            "similarity": 0,
            "searchMode": "embedding",
            "usingReRank": False,
            "datasetSearchUsingExtensionQuery": False,
        },
    )
    fast_db = replace(
        fast_app,
        name="fastgpt-retrieval",
        event=fast_retrieval,
        empty_workflow=fast_retrieval,
        metadata={"implementation": "FastGPT dataset searchTest API"},
    )

    max_base = (env.get("MAXKB_BASE_URL") or "http://127.0.0.1:8090").rstrip("/")
    max_app_id = env.get("MAXKB_APPLICATION_ID", "")
    max_event = ScenarioConfig(
        name="events",
        path=f"/chat/api/{max_app_id}/chat/completions" if max_app_id else "",
        protocol="sse",
        headers={"Accept": "*/*"},
        supported=bool(max_app_id),
        body={
            "model": env.get("MAXKB_MODEL", "maxkb"),
            "user": "lenovo-latency-{{uuid}}",
            "messages": [{"role": "user", "content": "{{query}}"}],
            "stream": True,
        },
    )
    max_app = TargetConfig(
        name="maxkb",
        base_url=max_base,
        api_key_env="MAXKB_API_KEY",
        auth_header="Authorization",
        event=max_event,
        empty_workflow=max_event,
        metadata={"implementation": "MaxKB published OpenAI-compatible API"},
    )
    max_knowledge_id = env.get("MAXKB_KNOWLEDGE_ID", "")
    max_retrieval = ScenarioConfig(
        name="retrieval",
        path=(
            f"/workspace/default/knowledge/{max_knowledge_id}/hit_test"
            if max_knowledge_id
            else ""
        ),
        protocol="json",
        supported=bool(max_knowledge_id),
        note="MaxKB admin hit-test diagnostic contract; embedding search with rerank disabled.",
        body={
            "query_text": "{{query}}",
            "top_number": 10,
            "similarity": 0.0,
            "search_mode": "embedding",
        },
    )
    max_db = TargetConfig(
        name="maxkb-retrieval",
        base_url=f"{max_base}/admin/api",
        api_key_env="MAXKB_ADMIN_TOKEN",
        auth_header="Authorization",
        event=max_retrieval,
        empty_workflow=max_retrieval,
        metadata={"implementation": "MaxKB admin knowledge hit_test diagnostic API"},
    )

    return {
        "moi": {"retrieval": moi_db, "events": moi_app},
        "dify": {"retrieval": dify_db, "events": dify_app},
        "fastgpt": {"retrieval": fast_db, "events": fast_app},
        "maxkb": {"retrieval": max_db, "events": max_app},
    }


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(dict(row), ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _write_samples(path: Path, samples: Sequence[Mapping[str, Any]]) -> None:
    _write_jsonl(path, samples)


def _run_api_track(
    platform: str,
    targets: Mapping[str, TargetConfig],
    queries: Sequence[Mapping[str, Any]],
    environ: Mapping[str, str],
    connections: int,
    timeout_s: float,
    output_dir: Path,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "platform": platform,
        "implementation": targets["events"].metadata.get("implementation"),
        "retrieval_contract": targets["retrieval"].metadata.get("implementation"),
        "connections_requested": connections,
    }
    warmup_target = targets["retrieval"] if targets["retrieval"].event.supported else targets["events"]
    result["retrieval_warmup"] = _warmup_retrieval(
        warmup_target, queries[0], environ, timeout_s
    ) if queries else {"status": "skipped", "reason": "no queries"}
    for track in ("retrieval", "events"):
        target = targets[track]
        scenario = target.event
        missing = target.missing_requirements(environ)
        if not scenario.supported:
            summary = _skipped_summary(
                scenario.note or f"{platform} {track} contract is not configured",
                connections,
            )
            samples: list[dict[str, Any]] = []
        elif missing:
            summary = _skipped_summary(
                f"missing local credential/config: {', '.join(missing)}", connections
            )
            samples = []
        else:
            summary, samples = run_request_batch(
                target,
                lambda query, scenario=scenario: replace_query(
                    scenario, str(query.get("question") or "")
                ),
                queries,
                environ,
                connections,
                timeout_s,
            )
            error_types = sorted(
                {str(sample.get("error")) for sample in samples if sample.get("error")}
            )
            if error_types:
                summary["error_types"] = error_types[:5]
            summary["status"] = _summary_status(summary)
        if track == "retrieval":
            result["retrieval"] = summary
            result["retrieval_latency_ms"] = summary.get("latency_ms", _distribution([]))
            _write_samples(output_dir / f"{platform}-retrieval-samples.jsonl", samples)
        else:
            result["events"] = summary
            _write_samples(output_dir / f"{platform}-event-samples.jsonl", samples)

    result["empty_workflow"] = unsupported_empty_workflow(
        "No explicit no-op workflow/application is configured for the Lenovo resources; "
        "the ready apps are real RAG apps, so an empty-workflow QPS would be misleading."
    )
    statuses = [result["retrieval"]["status"], result["events"]["status"]]
    result["status"] = "ok" if all(value == "ok" for value in statuses) else (
        "partial" if any(value in {"ok", "partial"} for value in statuses) else "error"
    )
    return result


def _moi_questions(path: Path, queries: Sequence[Mapping[str, Any]]) -> None:
    rows = []
    for index, query in enumerate(queries):
        question = str(query.get("question") or "")
        rows.append(
            {
                "id": str(query.get("question_id") or f"q-{index:04d}"),
                "question": question,
                "retrieval_keywords": [question],
                "relevant_documents": [],
                "relevant_evidence": [],
                "expected_answer_keywords": [],
            }
        )
    _write_jsonl(path, rows)


def _moi_latency_summary(
    rows: Sequence[Mapping[str, Any]],
    elapsed_s: float,
    connections: int,
    cli_first_output_samples: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    successful = [row for row in rows if str(row.get("status", "")).lower() == "ok"]
    latencies = []
    for row in successful:
        value = row.get("retrieval_latency_ms")
        if isinstance(value, (int, float)):
            latencies.append(float(value))
    cli_first_output_values = [
        float(sample["first_output_latency_ms"])
        for sample in cli_first_output_samples
        if str(sample.get("status", "")).lower() == "ok"
        and isinstance(sample.get("first_output_latency_ms"), (int, float))
    ]
    elapsed = max(elapsed_s, 0.000001)
    return {
        "status": "ok" if len(successful) == len(rows) and rows else (
            "partial" if successful else "error"
        ),
        "implementation": "native MatrixFlow SearchRAGChunks -> MatrixOne",
        "protocol": "cli",
        "requests": len(rows),
        "successes": len(successful),
        "errors": len(rows) - len(successful),
        "error_types": sorted(
            {str(row.get("error")) for row in rows if row.get("error")}
        )[:5],
        "qps": round(len(successful) / elapsed, 3),
        "retrieval_qps": round(len(successful) / elapsed, 3),
        "retrieval_latency_ms": _distribution(latencies),
        "connections": 1,
        "configured_connections": connections,
        "peak_in_flight": 1,
        "event_throughput_events_per_s": None,
        "ttfe_ms": _distribution([]),
        "latency_ms": _distribution(latencies),
        "cli_first_output_latency_ms": _distribution(cli_first_output_values),
    }


_MOI_ATTEMPT_OUTPUT_RE = re.compile(
    r"^attempt=(?P<attempt>\d+)/(?P<total>\d+) "
    r"id=(?P<question_id>\S+) repeat=(?P<repeat>\d+) "
    r"status=(?P<status>\S+)"
)


def _moi_cli_output_samples(
    captured_lines: Sequence[tuple[float, str]], started_at: float
) -> list[dict[str, Any]]:
    """Extract per-attempt first-output proxy samples from CLI output."""

    cumulative_times: list[float] = []
    samples: list[dict[str, Any]] = []
    for observed_at, line in captured_lines:
        match = _MOI_ATTEMPT_OUTPUT_RE.match(line.strip())
        if not match:
            continue
        cumulative_ms = round(max(observed_at - started_at, 0.0) * 1000, 3)
        values = match.groupdict()
        cumulative_times.append(cumulative_ms)
        samples.append(
            {
                "attempt": int(values["attempt"]),
                "total_attempts": int(values["total"]),
                "question_id": values["question_id"],
                "repeat": int(values["repeat"]),
                "status": values["status"],
                "cumulative_output_ms": cumulative_ms,
            }
        )
    intervals = cli_first_output_latencies(cumulative_times)
    for sample, interval in zip(samples, intervals):
        sample["first_output_latency_ms"] = interval
    return samples


def _run_moi(
    queries: Sequence[Mapping[str, Any]],
    environ: Mapping[str, str],
    connections: int,
    timeout_s: float,
    output_dir: Path,
) -> dict[str, Any]:
    # The Go runner changes its working directory to MOI_ROOT.  Resolve all
    # paths before invoking it so a relative benchmark output directory does
    # not make the generated question file disappear from the child process.
    output_dir = output_dir.resolve()
    question_path = output_dir / "moi-questions.jsonl"
    _moi_questions(question_path, queries)
    log_path = output_dir / "moi-run.log"
    command = [
        "go",
        "run",
        ".",
        "run",
        "--config",
        str(MOI_CONFIG),
        "--dataset",
        str(question_path),
        "--run",
        str(output_dir / "moi-native"),
        "--max-hits",
        "10",
        "--repeats",
        "1",
        "--attempt-timeout-seconds",
        str(max(1, int(timeout_s))),
    ]
    started = time.perf_counter()
    captured_lines: list[tuple[float, str]] = []
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=MOI_ROOT,
            env=dict(environ),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        def capture_output() -> None:
            if process is None or process.stdout is None:
                return
            for line in process.stdout:
                captured_lines.append((time.perf_counter(), line))

        reader = Thread(target=capture_output, name="moi-cli-output", daemon=True)
        reader.start()
        deadline = started + max(timeout_s * max(len(queries), 1) + 180, 300)
        while reader.is_alive():
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                process.kill()
                process.wait()
                reader.join(timeout=5)
                raise subprocess.TimeoutExpired(command, timeout=deadline - started)
            reader.join(timeout=min(0.2, remaining))
        return_code = process.wait()
        reader.join(timeout=5)
        combined = "".join(line for _, line in captured_lines)
        log_path.write_text(combined, encoding="utf-8")
    except subprocess.TimeoutExpired as exc:
        combined = f"MOI subprocess timeout: {exc}\n"
        if captured_lines:
            combined = "".join(line for _, line in captured_lines) + combined
        log_path.write_text(combined, encoding="utf-8")
        return {
            "platform": "moi",
            "status": "error",
            "implementation": "native MatrixFlow SearchRAGChunks -> MatrixOne",
            "protocol": "cli",
            "reason": "MOI local CLI timed out; see moi-run.log",
            "retrieval": _skipped_summary("MOI local CLI timed out", connections),
            "events": _skipped_summary("MOI local deployment has no streaming API in this implementation", connections),
            "empty_workflow": unsupported_empty_workflow(
                "MOI local implementation is a retrieval CLI; no no-op workflow endpoint is configured."
            ),
        }
    elapsed = max(time.perf_counter() - started, 0.000001)
    cli_first_output_samples = _moi_cli_output_samples(captured_lines, started)
    _write_samples(
        output_dir / "moi-cli-first-output-samples.jsonl", cli_first_output_samples
    )

    match = re.search(r"^run_dir=(.+)$", combined, re.MULTILINE)
    run_dir = Path(match.group(1).strip()) if match else None
    results_path = run_dir / "results.jsonl" if run_dir else None
    rows: list[dict[str, Any]] = []
    if results_path and results_path.is_file():
        for raw in results_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                rows.append(json.loads(raw))
    if not rows:
        reason = (
            f"MOI local CLI returned code {return_code} without results.jsonl; see moi-run.log"
        )
        return {
            "platform": "moi",
            "status": "error",
            "implementation": "native MatrixFlow SearchRAGChunks -> MatrixOne",
            "protocol": "cli",
            "reason": reason,
            "retrieval": _skipped_summary(reason, connections),
            "events": _skipped_summary(
                "MOI local implementation has no streaming HTTP API", connections
            ),
            "empty_workflow": unsupported_empty_workflow(
                "MOI local implementation is a retrieval CLI; no no-op workflow endpoint is configured."
            ),
        }
    retrieval = _moi_latency_summary(
        rows, elapsed, connections, cli_first_output_samples
    )
    retrieval["run_dir"] = str(run_dir) if run_dir else None
    _write_samples(output_dir / "moi-samples.jsonl", rows)
    return {
        "platform": "moi",
        "status": retrieval["status"] if return_code == 0 else "partial",
        "implementation": "native MatrixFlow SearchRAGChunks -> MatrixOne",
        "protocol": "cli",
        "retrieval": retrieval,
        "retrieval_latency_ms": retrieval["retrieval_latency_ms"],
        "cli_first_output_latency_ms": retrieval["cli_first_output_latency_ms"],
        "events": {
            "status": "unsupported",
            "reason": "MOI local implementation exposes the native retrieval CLI, not a streaming HTTP API.",
            "event_throughput_events_per_s": None,
            "ttfe_ms": _distribution([]),
            "ttfe_proxy": {
                "status": "ok" if cli_first_output_samples else "unsupported",
                "name": "CLI First Output Latency",
                "latency_ms": retrieval["cli_first_output_latency_ms"],
                "reason": "first terminal CLI attempt output, not a streaming HTTP event",
            },
            "connections": None,
            "peak_in_flight": None,
        },
        "empty_workflow": unsupported_empty_workflow(
            "MOI local implementation is a retrieval CLI; no no-op workflow endpoint is configured."
        ),
    }


def _run_moi_with_official_api(
    targets: Mapping[str, TargetConfig],
    queries: Sequence[Mapping[str, Any]],
    environ: Mapping[str, str],
    connections: int,
    timeout_s: float,
    output_dir: Path,
) -> dict[str, Any]:
    result = _run_moi(queries, environ, connections, timeout_s, output_dir)
    api_result = _run_api_track(
        "moi", targets, queries, environ, connections, timeout_s, output_dir
    )
    result["implementation"] = "MOI official Catalog A2A SSE + native SearchRAGChunks diagnostic"
    result["protocol"] = "a2a-sse + native-cli"
    result["events"] = api_result["events"]
    result["retrieval_warmup"] = api_result["retrieval_warmup"]
    statuses = [result.get("retrieval", {}).get("status"), result["events"].get("status")]
    result["status"] = "ok" if all(value == "ok" for value in statuses) else "partial"
    return result


def _value(value: Any, digits: int = 3) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _p(summary: Mapping[str, Any], key: str, percentile: str) -> Any:
    value = summary.get(key) or {}
    return value.get(percentile) if isinstance(value, Mapping) else None


def _make_report(
    output_dir: Path,
    queries: Sequence[Mapping[str, Any]],
    seed: int,
    connections: int,
    timeout_s: float,
    platform_execution: str,
    results: Mapping[str, Mapping[str, Any]],
    platforms: Sequence[str] = PLATFORM_ORDER,
) -> str:
    tested_platforms = tuple(platforms)
    lines = [
        "# Lenovo local RAG latency benchmark",
        "",
        "This report measures the checked-in local Lenovo resources with ten reproducibly sampled queries. Answer quality is intentionally not evaluated.",
        "",
        "## Technical summary",
        "",
        f"- Query source: `{DATASET_DEFAULT}` (sample size `{len(queries)}`, seed `{seed}`).",
        f"- Platforms in this pass: `{', '.join(tested_platforms)}`.",
        f"- API concurrency: `{connections}` fresh HTTP connections per product; platform execution mode is `{platform_execution}`.",
        f"- Per-request timeout: `{timeout_s:g}` seconds.",
        "- The primary database-facing track is direct retrieval. Each HTTP product gets one uncounted direct-retrieval warmup probe; the streaming/application track is only for transport/headline metrics.",
        "",
        "## Headline results",
        "",
        "| Platform | Retrieval p50 / p95 (ms) | App protocol | App QPS | TTFB p50 / p95 (ms) | TTFE p50 / p95 (ms) | Event Throughput* (events/s) | Connections (configured / peak / avg active) | Empty Workflow QPS | Status |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for platform in tested_platforms:
        result = results.get(platform, {})
        retrieval = result.get("retrieval", {})
        events = result.get("events", {})
        empty = result.get("empty_workflow", {})
        retrieval_display = (
            f"{_value(_p(retrieval, 'latency_ms', 'p50'))} / "
            f"{_value(_p(retrieval, 'latency_ms', 'p95'))}"
        )
        event_usable = (
            events.get("status") not in {"error", "unsupported", "skipped"}
            and int(events.get("successes", 0) or 0) > 0
        )
        protocol = events.get("protocol")
        throughput = (
            _value(events.get("event_throughput_events_per_s"))
            if event_usable and protocol == "sse"
            else "N/A"
        )
        ttfb = (
            f"{_value(_p(events, 'ttfb_ms', 'p50'))} / {_value(_p(events, 'ttfb_ms', 'p95'))}"
            if event_usable
            else "N/A / N/A"
        )
        ttfe = (
            f"{_value(_p(events, 'ttfe_ms', 'p50'))} / {_value(_p(events, 'ttfe_ms', 'p95'))}"
            if event_usable and protocol == "sse"
            else "N/A / N/A"
        )
        configured = events.get("connections", result.get("connections_requested")) if event_usable else None
        peak = events.get("peak_in_flight") if event_usable else None
        average = events.get("average_in_flight") if event_usable else None
        connections_display = (
            f"{_value(configured, 0)} / {_value(peak, 0)} / {_value(average)}"
        )
        lines.append(
            f"| {platform.upper()} | {retrieval_display} | {_value(protocol)} | "
            f"{_value(events.get('qps')) if event_usable else 'N/A'} | {ttfb} | {ttfe} | "
            f"{throughput} | {connections_display} | {_value(empty.get('qps'))} | "
            f"{result.get('status', 'error')} |"
        )

    lines += [
        "",
        "Notes: `N/A` means the local implementation or test resource does not expose a truthful contract for that metric; it is not a measured zero.",
        "",
        "## Scope, data, and contracts",
        "",
        "| Platform | Lenovo resource | Database-facing contract | Streaming contract |",
        "|---|---|---|---|",
        "| MOI | `moi_stage1_lenovo_bench.embedding_results` + official benchmark workspace | Native `SearchRAGChunks` CLI over MatrixOne (separate diagnostic) | Official Catalog `POST /api/v1/agents/a2a`, A2A SSE |",
        "| Dify | Dataset resource `DIFY_DATASET_ID` | `POST /v1/datasets/{id}/retrieve`, semantic search, rerank off | `POST /v1/chat-messages`, SSE |",
        "| FastGPT | Lenovo native app/dataset manifest | `POST /api/core/dataset/searchTest`, embedding mode, rerank off | `POST /api/v1/chat/completions`, SSE |",
        "| MaxKB | Ready chunked-text knowledge resource | Admin `hit_test`, embedding mode; diagnostic contract | Published OpenAI-compatible app, SSE (`stream=true`) |",
        "",
        "## Metric definitions and methodology",
        "",
        "- **Retrieval latency**: end-to-end elapsed time for the direct retrieval request, including query embedding and local service/database overhead where that product contract performs embedding. It is the closest comparable database-path measurement available without instrumenting each product internally.",
        "- **Application QPS**: application-successful requests divided by wall-clock batch time. An HTTP 2xx stream containing an explicit `error` event is not successful.",
        "- **TTFB / TTFE**: TTFB is time to the first response byte for JSON or SSE; TTFE is time to the first complete SSE event and is N/A for non-streaming JSON.",
        "- **Event Throughput**: complete SSE events divided by wall-clock batch time. It is implementation-specific because products frame tokens and workflow state differently, so it must not be used for direct cross-product ranking.",
        "- **Connections**: configured parallel workers and observed peak in-flight requests. Each request uses a fresh HTTP connection, so timings include connection setup and are not keep-alive latency.",
        "- **Empty Workflow QPS**: intentionally unsupported until each product has an explicit no-op workflow/application. Calling a real Lenovo RAG app with an empty prompt would measure retrieval/generation behavior, not an empty workflow.",
        "- Each platform receives the same ten query texts, exactly once per measured direct-retrieval and application track; one separate readiness warmup is not included in the ten samples. No answer text is scored or compared.",
        "",
        "## Robustness and limitations",
        "",
        "- The sample is random but fixed by the recorded seed; rerun with another seed for confidence intervals. Ten requests are suitable for a smoke/latency pass, not a production capacity claim.",
        "- API streaming numbers include application orchestration, model/provider time, and transport buffering. They should not be read as pure vector-database latency. A Dify-style capacity test also requires a deterministic mock LLM rather than the external MaaS model used by this Lenovo pass.",
        "- The MaxKB retrieval call is an admin diagnostic `hit_test` contract, while the other direct retrieval calls are product API contracts; this difference is recorded to avoid overstating equivalence.",
        "- MOI's Event Throughput, TTFE and API Connections come only from the official A2A/SSE data plane. The CLI is retained solely for direct MatrixFlow/MatrixOne retrieval latency and is not HTTP-wrapped.",
        "- A platform marked `partial`, `error`, or `skipped` needs a deployment/credential fix before cross-platform ranking.",
        "",
        "## Runtime status and blockers",
        "",
    ]
    for platform in tested_platforms:
        result = results.get(platform, {})
        warmup = result.get("retrieval_warmup") or {}
        lines.append(
            f"- **{platform.upper()}** overall status: `{result.get('status', 'error')}`; "
            f"retrieval warmup: `{warmup.get('status', 'not applicable')}`."
        )
        for track in ("retrieval", "events"):
            summary = result.get(track) or {}
            if summary.get("status") in {"ok", None}:
                continue
            detail = summary.get("reason") or "; ".join(summary.get("error_types") or [])
            if detail:
                lines.append(f"  - `{track}`: {detail}")
    lines += [
        "",
        "## Artifacts and next steps",
        "",
        f"- Selected queries: `selected-queries.jsonl` in `{output_dir}`.",
        f"- Machine-readable results: `results.json` in `{output_dir}`.",
        f"- Per-track samples and MOI logs are stored beside this report; credentials are not written to the artifacts.",
        "- For a memory-constrained host, run one platform at a time with `--platforms`; keep the same seed, query count, connection count, and timeout for every pass. For a genuine Empty Workflow QPS row, create one no-op app/workflow per product and provide its endpoint/resource ID explicitly.",
        "",
    ]
    return "\n".join(lines)


def run_benchmark(args: argparse.Namespace) -> Path:
    rows = load_query_rows(Path(args.questions))
    queries = select_queries(rows, args.count, args.seed)
    output_dir = Path(args.output) if args.output else (
        ROOT / "runs/lenovo-local-latency" / time.strftime("%Y%m%d-%H%M%S")
    )
    if output_dir.exists():
        output_dir = output_dir.with_name(output_dir.name + f"-{uuid.uuid4().hex[:6]}")
    output_dir.mkdir(parents=True, exist_ok=False)
    _write_jsonl(output_dir / "selected-queries.jsonl", queries)

    environ = enrich_resource_env(load_local_env())
    api_targets = build_lenovo_api_targets(environ)
    jobs: dict[str, Callable[[], dict[str, Any]]] = {
        platform: lambda platform=platform, targets=targets: _run_api_track(
            platform,
            targets,
            queries,
            environ,
            args.connections,
            args.timeout,
            output_dir,
        )
        for platform, targets in api_targets.items()
    }
    jobs["moi"] = lambda: _run_moi_with_official_api(
        api_targets["moi"], queries, environ, args.connections, args.timeout, output_dir
    )

    results: dict[str, dict[str, Any]] = {}
    platform_execution = getattr(args, "platform_execution", "serial")
    platforms = tuple(getattr(args, "platforms", PLATFORM_ORDER))

    def run_one(name: str) -> None:
        try:
            results[name] = jobs[name]()
        except Exception as exc:  # preserve a report even if one adapter breaks
            results[name] = {
                "platform": name,
                "status": "error",
                "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
                "retrieval": _skipped_summary(str(exc), args.connections),
                "events": _skipped_summary(str(exc), args.connections),
                "empty_workflow": unsupported_empty_workflow(
                    "No explicit no-op workflow/application configured."
                ),
            }

    platform_order = platforms
    if platform_execution == "serial":
        for name in platform_order:
            run_one(name)
    else:
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_map = {
                executor.submit(run_one, name): name for name in platform_order
            }
            for future in as_completed(future_map):
                future.result()

    manifest = {
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "questions": str(Path(args.questions).resolve()),
        "count": len(queries),
        "seed": args.seed,
        "connections": args.connections,
        "timeout_seconds": args.timeout,
        "platform_execution": platform_execution,
        "platforms": list(platforms),
        "quality_evaluation": False,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "results.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = _make_report(
        output_dir,
        queries,
        args.seed,
        args.connections,
        args.timeout,
        platform_execution,
        results,
        platforms,
    )
    report_path = output_dir / "report.md"
    report_path.write_text(report, encoding="utf-8")
    return report_path


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=DATASET_DEFAULT)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20260814)
    parser.add_argument("--connections", type=int, default=4)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument(
        "--platforms",
        nargs="+",
        choices=PLATFORM_ORDER,
        default=list(PLATFORM_ORDER),
        help="platforms to include in this pass; default runs all four in serial order",
    )
    parser.add_argument(
        "--platform-execution",
        choices=("serial", "parallel"),
        default="serial",
        help="run product jobs serially (default) or in parallel; per-product API connections remain concurrent",
    )
    parser.add_argument("--output", type=Path, default=None)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report_path = run_benchmark(args)
    print(f"report={report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
