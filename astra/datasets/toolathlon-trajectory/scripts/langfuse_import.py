"""Prepare, import and verify cleaned Astra Toolathlon trajectories in Langfuse."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
from urllib.parse import urlencode
import uuid

ROOT = Path(__file__).resolve().parents[4]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TERMINAL_IMPORTER = (
    ROOT
    / "astra/datasets/linux-terminal-bench-trajectory/scripts/langfuse_import.py"
)
spec = importlib.util.spec_from_file_location("terminal_bench_langfuse_import", TERMINAL_IMPORTER)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load shared Langfuse client: {TERMINAL_IMPORTER}")
terminal_importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(terminal_importer)
Client = terminal_importer.Client
attr = terminal_importer.attr
decode_io = terminal_importer.decode_io
dump = terminal_importer.dump
nanos = terminal_importer.nanos


DEFAULT_DATASET = ROOT / "astra/datasets/toolathlon-trajectory"
SERVICE_NAME = "toolathlon-astra-offline"
METADATA_EXPANSIONS = (
    "agent,benchmark,usage,timing,quality,source,trial,message_details,"
    "missing_fields,token_usage_coverage"
)


def chat_messages(messages):
    output = []
    for message in messages:
        row = {"role": message["role"], "content": message.get("content") or ""}
        if message.get("reasoning_content"):
            row["reasoning_content"] = message["reasoning_content"]
        if message.get("tool_calls"):
            row["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": call["name"],
                        "arguments": call["arguments_json"],
                    },
                }
                for call in message["tool_calls"]
            ]
        if message.get("tool_call_id") is not None:
            row["tool_call_id"] = message["tool_call_id"]
        if message.get("tool_name"):
            row["name"] = message["tool_name"]
        output.append(row)
    return output


def missing_fields(value, prefix):
    missing = []
    if value is None:
        return [prefix]
    if isinstance(value, dict):
        for key, item in value.items():
            missing.extend(missing_fields(item, f"{prefix}.{key}"))
    return missing


def stable_ids(batch_id, record_id):
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"toolathlon:{batch_id}")
    trace_id = uuid.uuid5(namespace, f"trace:{record_id}").hex
    span_id = uuid.uuid5(namespace, f"observation:{record_id}").hex[:16]
    score_id = str(uuid.uuid5(namespace, f"score:runner_reward:{record_id}"))
    return trace_id, span_id, score_id


def validate_record(record, tier):
    required = {"record_id", "agent", "benchmark", "trial", "instruction", "messages", "outcome", "usage", "timing", "quality", "source"}
    missing = sorted(required - set(record))
    if missing:
        raise ValueError(f"{record.get('record_id', '<unknown>')}: missing fields {missing}")
    if record["quality"].get("tier") != tier:
        raise ValueError(f"{record['record_id']}: quality tier does not match data split")
    outcome = record["outcome"]
    if outcome.get("evaluator_valid") is not True or not isinstance(outcome.get("evaluator_pass"), bool):
        raise ValueError(f"{record['record_id']}: evaluator is not valid")
    expected = "pass" if outcome["evaluator_pass"] else "no_pass"
    if outcome.get("status") != expected:
        raise ValueError(f"{record['record_id']}: evaluator status is inconsistent")
    if not isinstance(record["messages"], list) or not record["messages"]:
        raise ValueError(f"{record['record_id']}: messages are missing")


def read_dataset(dataset):
    records = []
    seen = set()
    for tier in ("complete", "partial"):
        path = dataset / "data" / tier / "astra.jsonl"
        if not path.is_file():
            raise ValueError(f"Cleaned split not found: {path}")
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"Invalid JSON at {path}:{line_number}: {exc.msg}") from None
                if not isinstance(record, dict):
                    raise ValueError(f"Expected object at {path}:{line_number}")
                validate_record(record, tier)
                if record["record_id"] in seen:
                    raise ValueError(f"Duplicate record_id: {record['record_id']}")
                seen.add(record["record_id"])
                records.append(record)
    records.sort(key=lambda record: record["record_id"])
    return records


def make_item(record, batch_id):
    task = record["benchmark"]["task_id"]
    trace_id, span_id, score_id = stable_ids(batch_id, record["record_id"])
    started_at = record["trial"].get("started_at")
    finished_at = record["trial"].get("finished_at")
    if not started_at or not finished_at:
        raise ValueError(f"{record['record_id']}: trial boundaries are missing")
    start, end = nanos(started_at), nanos(finished_at)
    if int(end) < int(start):
        raise ValueError(f"{record['record_id']}: negative trial duration")
    reward = 1 if record["outcome"]["evaluator_pass"] else 0
    usage_missing = missing_fields(record["usage"], "usage")
    timing_missing = missing_fields(record["timing"], "timing")
    metadata = {
        "import_batch": batch_id,
        "record_id": record["record_id"],
        "product": "astra",
        "task": task,
        "model": record["agent"].get("model"),
        "attempt_run_id": record["trial"].get("attempt_run_id"),
        "selection_kind": record["benchmark"].get("selection_kind"),
        "selection_manifest_selected": True,
        "terminal_status": record["trial"].get("terminal_status"),
        "evaluator_status": record["outcome"]["status"],
        "evaluator_pass": record["outcome"]["evaluator_pass"],
        "evaluator_valid": True,
        "evaluation_kind": record["outcome"].get("evaluation_kind"),
        "reward": reward,
        "quality_tier": record["quality"]["tier"],
        "reasoning_included": record["quality"].get("reasoning_included", False),
        "token_usage_affects_tier": False,
        "token_usage_coverage": record["usage"].get("coverage"),
        "missing_fields": usage_missing + timing_missing + [
            "per_model_call_observation",
            "per_tool_call_span",
        ],
        "usage": record["usage"],
        "timing": record["timing"],
        "benchmark": record["benchmark"],
        "agent": record["agent"],
        "quality": record["quality"],
        "source": record["source"],
        "trial": record["trial"],
        "message_details": [
            {
                key: message.get(key)
                for key in (
                    "seq",
                    "timestamp",
                    "is_error",
                    "segment_index",
                    "internal_content_omitted",
                )
            }
            for message in record["messages"]
        ],
    }
    attributes = [
        attr("langfuse.trace.name", f"astra/{task}"),
        attr(
            "langfuse.trace.tags",
            [
                "toolathlon",
                "astra",
                record["quality"]["tier"],
                f"selection:{record['benchmark'].get('selection_kind')}",
                f"import:{batch_id}",
            ],
        ),
        attr("langfuse.environment", "benchmark-offline"),
        attr("langfuse.observation.type", "agent"),
        attr(
            "langfuse.observation.input",
            dump([{"role": "user", "content": record["instruction"]}]),
        ),
        attr("langfuse.observation.output", dump(chat_messages(record["messages"]))),
    ] + [attr(f"langfuse.trace.metadata.{key}", value) for key, value in metadata.items()]
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": f"astra/{task}",
        "kind": 1,
        "startTimeUnixNano": start,
        "endTimeUnixNano": end,
        "attributes": attributes,
    }
    score = {
        "id": score_id,
        "traceId": trace_id,
        "observationId": span_id,
        "name": "runner_reward",
        "value": reward,
        "dataType": "NUMERIC",
        "environment": "benchmark-offline",
        "comment": "valid Toolathlon evaluator result from the selected attempt",
        "metadata": {
            "import_batch": batch_id,
            "record_id": record["record_id"],
            "selection_kind": record["benchmark"].get("selection_kind"),
        },
    }
    return {
        "record_id": record["record_id"],
        "trace_id": trace_id,
        "span_id": span_id,
        "reward": reward,
        "evaluator_status": record["outcome"]["status"],
        "metadata": metadata,
        "payload": {
            "resourceSpans": [
                {
                    "resource": {"attributes": [attr("service.name", SERVICE_NAME)]},
                    "scopeSpans": [
                        {"scope": {"name": SERVICE_NAME}, "spans": [span]}
                    ],
                }
            ]
        },
        "scores": [score],
    }


def atomic_bundle(path, report, items):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(dump({"report": report}) + "\n")
        for item in items:
            stream.write(dump(item) + "\n")
    temporary.replace(path)


def prepare(args):
    dataset = args.dataset.resolve()
    records = read_dataset(dataset)
    batch_id = str(uuid.uuid4())
    items = [make_item(record, batch_id) for record in records]
    rewards = [item["reward"] for item in items]
    report = {
        "import_batch": batch_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(dataset),
        "traces": len(items),
        "scores": sum(len(item["scores"]) for item in items),
        "complete": sum(record["quality"]["tier"] == "complete" for record in records),
        "partial": sum(record["quality"]["tier"] == "partial" for record in records),
        "outcomes": dict(Counter(record["outcome"]["status"] for record in records)),
        "selection_kinds": dict(
            Counter(record["benchmark"].get("selection_kind") for record in records)
        ),
        "reasoning_traces": sum(
            bool(record["quality"].get("reasoning_included")) for record in records
        ),
        "token_usage_coverage": dict(
            Counter((record["usage"].get("coverage") or {}).get("status") for record in records)
        ),
        "selected_evaluator_mean_reward": sum(rewards) / len(rewards) if rewards else None,
        "score_name": "runner_reward",
        "scope": "one selected, evaluator-valid Astra attempt per retained Toolathlon task",
    }
    atomic_bundle(args.output, report, items)
    print(f"Prepared {len(items)} traces and {report['scores']} scores: {args.output}")


def read_bundle(path):
    with path.open(encoding="utf-8") as stream:
        first = next(stream, None)
        if first is None:
            raise ValueError(f"Empty bundle: {path}")
        try:
            report = json.loads(first)["report"]
            items = [json.loads(line) for line in stream if line.strip()]
        except (json.JSONDecodeError, KeyError, TypeError):
            raise ValueError(f"Invalid bundle: {path}") from None
    if report.get("traces") != len(items):
        raise ValueError("Bundle report trace count does not match its items")
    return report, items


def upload(args):
    report, items = read_bundle(args.bundle)
    client = Client()
    for index, item in enumerate(items, 1):
        client.request("POST", "/api/public/otel/v1/traces", item["payload"])
        for score in item["scores"]:
            client.request("POST", "/api/public/scores", score)
        if index % 10 == 0 or index == len(items):
            print(f"Accepted {index}/{len(items)} traces", flush=True)
    print(f"API accepted batch {report['import_batch']}; run verify to confirm persistence")


def comparable_metadata(actual, expected):
    if isinstance(actual, str) and isinstance(expected, (dict, list)):
        try:
            actual = json.loads(actual)
        except json.JSONDecodeError:
            pass
    return actual == expected


def verify(args):
    report, items = read_bundle(args.bundle)
    client = Client()
    failures = []
    for index, item in enumerate(items, 1):
        span = item["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        expected_output = next(
            attribute["value"]["stringValue"]
            for attribute in span["attributes"]
            if attribute["key"] == "langfuse.observation.output"
        )
        started = datetime.fromtimestamp(int(span["startTimeUnixNano"]) / 1e9, timezone.utc)
        query = {
            "traceId": item["trace_id"],
            "limit": 100,
            "fromStartTime": (started - timedelta(seconds=1)).isoformat(),
            "toStartTime": (started + timedelta(seconds=1)).isoformat(),
        }
        if args.api_version == "v4":
            query["fields"] = "core,basic,io,metadata"
            query["expandMetadata"] = METADATA_EXPANSIONS
            observation_endpoint = "/api/public/v2/observations"
            score_endpoint = "/api/public/v3/scores"
        else:
            observation_endpoint = "/api/public/observations"
            score_endpoint = "/api/public/v2/scores"
        rows = client.request(
            "GET", observation_endpoint + "?" + urlencode(query)
        ).get("data", [])
        row = next((candidate for candidate in rows if candidate.get("id") == item["span_id"]), None)
        if row is None:
            failures.append({"record_id": item["record_id"], "reason": "missing observation"})
        else:
            if decode_io(row.get("output")) != decode_io(expected_output):
                failures.append({"record_id": item["record_id"], "reason": "output mismatch"})
            for field, source_field in (
                ("startTime", "startTimeUnixNano"),
                ("endTime", "endTimeUnixNano"),
            ):
                if not row.get(field) or int(nanos(row[field])) // 10**6 != int(
                    span[source_field]
                ) // 10**6:
                    failures.append(
                        {"record_id": item["record_id"], "reason": f"timestamp mismatch: {field}"}
                    )
            actual_metadata = row.get("metadata") or {}
            for field, expected in item["metadata"].items():
                if field not in actual_metadata or not comparable_metadata(
                    actual_metadata[field], expected
                ):
                    failures.append(
                        {"record_id": item["record_id"], "reason": f"metadata mismatch: {field}"}
                    )
        score_rows = client.request(
            "GET",
            score_endpoint
            + "?"
            + urlencode({"traceId": item["trace_id"], "limit": 100}),
        ).get("data", [])
        for expected in item["scores"]:
            actual = next(
                (candidate for candidate in score_rows if candidate.get("id") == expected["id"]),
                None,
            )
            if (
                actual is None
                or actual.get("name") != expected["name"]
                or actual.get("value") != expected["value"]
            ):
                failures.append(
                    {
                        "record_id": item["record_id"],
                        "reason": f"missing/mismatched score {expected['name']}",
                    }
                )
        if index % 10 == 0 or index == len(items):
            print(f"Verified {index}/{len(items)} traces", flush=True)
    result = {
        "import_batch": report["import_batch"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checked_traces": len(items),
        "checked_scores": sum(len(item["scores"]) for item in items),
        "failures": failures,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.report.with_name(args.report.name + ".tmp")
    temporary.write_text(dump(result) + "\n", encoding="utf-8")
    temporary.replace(args.report)
    print(f"Verification failures: {len(failures)}; report: {args.report}")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser(
        "prepare", help="Build an immutable import bundle from cleaned Toolathlon JSONL"
    )
    prepare_parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    prepare_parser.add_argument("--output", type=Path, required=True)
    import_parser = subparsers.add_parser(
        "import", help="Upload a prepared bundle using its existing IDs"
    )
    import_parser.add_argument("--bundle", type=Path, required=True)
    verify_parser = subparsers.add_parser(
        "verify", help="Read back every observation, metadata object and score"
    )
    verify_parser.add_argument("--bundle", type=Path, required=True)
    verify_parser.add_argument("--api-version", choices=["v3", "v4"], default="v4")
    verify_parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        return {"prepare": prepare, "import": upload, "verify": verify}[args.command](args) or 0
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
