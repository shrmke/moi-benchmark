"""Prepare, import and verify offline Terminal-Bench trajectories in Langfuse."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
from http.client import HTTPException
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
import uuid

DATASET = Path(__file__).resolve().parent.parent
PRODUCTS = ("astra", "dsh", "hermes", "pi")

def dump(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def nanos(value):
    # Harbor writes naive UTC timestamps; do not interpret them in host local time.
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = dt - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return str((delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000)


def attr(key, value):
    if isinstance(value, bool):
        encoded = {"boolValue": value}
    elif isinstance(value, int):
        encoded = {"intValue": str(value)}
    elif isinstance(value, float):
        encoded = {"doubleValue": value}
    elif isinstance(value, list) and all(isinstance(x, str) for x in value):
        encoded = {"arrayValue": {"values": [{"stringValue": x} for x in value]}}
    else:
        encoded = {"stringValue": value if isinstance(value, str) else dump(value)}
    return {"key": key, "value": encoded}


def chat_messages(messages):
    output = []
    for message in messages:
        row = {"role": message["role"], "content": message["content"]}
        if message.get("tool_calls"):
            row["tool_calls"] = [
                {"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": call["arguments_json"]}}
                for call in message["tool_calls"]
            ]
        if message.get("tool_call_id") is not None:
            row["tool_call_id"] = message["tool_call_id"]
        if message.get("tool_name"):
            row["name"] = message["tool_name"]
        output.append(row)
    return output



def record_reward(record):
    reward = (record.get("outcome") or {}).get("reward")
    if isinstance(reward, bool) or not isinstance(reward, (int, float)) or reward not in (0, 1):
        return None
    return int(reward)


def make_item(record, batch_id):
    outcome = record.get("outcome") or {}
    reward = record_reward(record)
    status = "passed" if reward == 1 else "failed" if reward == 0 else "invalid_verifier"
    product = record["agent"]["product"]
    task = record["benchmark"]["task_id"]
    trace_id, span_id = uuid.uuid4().hex, uuid.uuid4().hex[:16]
    start, end = nanos(record["trial"]["started_at"]), nanos(record["trial"]["finished_at"])
    if int(end) < int(start):
        raise ValueError(f"negative trial duration: {record['record_id']}")
    passed, failed = outcome.get("verifier_passed"), outcome.get("verifier_failed")
    test_count = passed + failed if type(passed) is int and type(failed) is int else None
    metadata = {
        "import_batch": batch_id, "record_id": record["record_id"],
        "product": product, "task": task, "model": record["agent"]["model_name"],
        "attempt_index": record["trial"]["attempt_index"],
        "verifier_status": status, "reward": reward,
        "raw_reward": outcome.get("raw_structured_reward"),
        "verifier_test_count": test_count,
        "verifier_passed": passed, "verifier_failed": failed,
        "verifier_details": record.get("verifier"),
        "quality_tier": record["quality"]["tier"],
        "quality_basis": "cleaned dataset; score uses outcome.reward",
        "missing_fields": [f"usage.{k}" for k, v in record["usage"].items() if v is None]
            + [f"timing.{k}" for k, v in record["timing"].items() if v is None]
            + [key for key in ("verifier_passed", "verifier_failed") if outcome.get(key) is None]
            + ["per_call_start_end", "per_call_usage"],
        "usage": record["usage"], "timing": record["timing"],
        "benchmark": record["benchmark"], "agent": record["agent"],
        "quality": record["quality"], "source": record["source"],
        "message_details": [{k: m[k] for k in (
            "seq", "timestamp", "is_error", "internal_content_omitted")}
            for m in record["messages"]],
    }
    attributes = [
        attr("langfuse.trace.name", f"{product}/{task}"),
        attr("langfuse.trace.tags", ["linux-terminal-bench", product,
             record["quality"]["tier"], f"import:{batch_id}"]),
        attr("langfuse.environment", "benchmark-offline"),
        attr("langfuse.observation.type", "agent"),
        attr("langfuse.observation.input", dump([{"role": "user", "content": record["instruction"]}])),
        attr("langfuse.observation.output", dump(chat_messages(record["messages"]))),
    ] + [attr(f"langfuse.trace.metadata.{key}", value) for key, value in metadata.items()]
    span = {"traceId": trace_id, "spanId": span_id, "name": f"{product}/{task}",
            "kind": 1, "startTimeUnixNano": start, "endTimeUnixNano": end,
            "attributes": attributes}
    scores = []
    if reward is not None:
        scores.append({"id": str(uuid.uuid4()), "traceId": trace_id,
                       "observationId": span_id, "name": "runner_reward", "value": reward,
                       "dataType": "NUMERIC", "environment": "benchmark-offline",
                       "comment": "cleaned dataset outcome.reward; filter one import_batch",
                       "metadata": {"import_batch": batch_id, "record_id": record["record_id"]}})
    return {"record_id": record["record_id"], "trace_id": trace_id,
            "span_id": span_id, "reward": reward, "verifier_status": status,
            "payload": {"resourceSpans": [{"resource": {"attributes": [
                attr("service.name", "linux-terminal-bench-offline")]},
                "scopeSpans": [{"scope": {"name": "linux-terminal-bench-offline"}, "spans": [span]}]}]},
            "scores": scores}


def prepare(args):
    dataset = args.dataset.resolve()
    if not dataset.is_dir():
        raise ValueError(f"Dataset directory not found: {dataset}")
    dataset_report = json.loads((dataset / "quality_report.json").read_text())
    batch_id = str(uuid.uuid4())
    report = {"import_batch": batch_id, "generated_at": datetime.now(timezone.utc).isoformat(),
              "dataset": str(dataset), "products": {}}
    items = []
    for product in args.products:
        records = []
        for tier in ("complete", "partial"):
            with (dataset / "data" / tier / f"{product}.jsonl").open() as stream:
                records.extend(json.loads(line) for line in stream if line.strip())
        if any(record["agent"]["product"] != product for record in records):
            raise ValueError(f"Dataset product mismatch: {product}")
        product_items = [make_item(record, batch_id) for record in records]
        scored = [item for item in product_items if item["reward"] is not None]
        report["products"][product] = {
            "quality": dataset_report["products"][product], "traces": len(product_items),
            "scored_traces": len(scored),
            "passed_traces": sum(item["reward"] == 1 for item in scored),
            "mean_reward": sum(item["reward"] for item in scored) / len(scored) if scored else None,
        }
        items.extend(product_items)
        print(f"{product}: {len(product_items)} traces, {len(scored)} scored", flush=True)
    report["traces"] = len(items)
    report["scores"] = sum(len(item["scores"]) for item in items)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as stream:
        stream.write(dump({"report": report}) + "\n")
        for item in items:
            stream.write(dump(item) + "\n")
    print(f"Prepared {len(items)} traces: {args.output}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self):
        self.base = os.environ.get("LANGFUSE_BASE_URL", "").rstrip("/")
        public = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret = os.environ.get("LANGFUSE_SECRET_KEY")
        if not self.base or not public or not secret:
            raise ValueError("Set LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY")
        url = urlsplit(self.base)
        if url.scheme not in {"http", "https"} or not url.netloc or url.username or url.query or url.fragment:
            raise ValueError("LANGFUSE_BASE_URL must be an http(s) base URL without credentials/query/fragment")
        self.auth = base64.b64encode(f"{public}:{secret}".encode()).decode()
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, body=None):
        data = dump(body).encode() if body is not None else None
        req = Request(self.base + path, data=data, method=method, headers={
            "Authorization": "Basic " + self.auth, "Content-Type": "application/json",
            "x-langfuse-ingestion-version": "4"})
        for attempt in range(4):
            try:
                with self.opener.open(req, timeout=45) as response:
                    raw = response.read()
                    value = json.loads(raw) if raw else {}
                partial = value.get("partialSuccess", {})
                if int(partial.get("rejectedSpans", 0)) or partial.get("errorMessage"):
                    raise RuntimeError("OTLP returned partial success; import not complete")
                return value
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise RuntimeError(f"Langfuse HTTP {exc.code} on {method} {path.split('?')[0]}") from None
            except (URLError, TimeoutError, ConnectionError, HTTPException):
                if attempt == 3:
                    raise RuntimeError("Langfuse network request failed after retries") from None
            time.sleep(2 ** attempt)


def read_bundle(path):
    with path.open(encoding="utf-8") as stream:
        report = json.loads(next(stream))["report"]
        items = [json.loads(line) for line in stream if line.strip()]
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
    print(f"API accepted batch {report['import_batch']}; run verify to confirm stored data")


def decode_io(value):
    return json.loads(value) if isinstance(value, str) else value


def verify(args):
    report, items = read_bundle(args.bundle)
    client = Client()
    failures = []
    for index, item in enumerate(items, 1):
        span = item["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        expected_output = next(a["value"]["stringValue"] for a in span["attributes"]
                               if a["key"] == "langfuse.observation.output")
        dt = datetime.fromtimestamp(int(span["startTimeUnixNano"]) / 1e9, timezone.utc)
        query = {"traceId": item["trace_id"], "limit": 100,
                 "fromStartTime": (dt - timedelta(seconds=1)).isoformat(),
                 "toStartTime": (dt + timedelta(seconds=1)).isoformat()}
        if args.api_version == "v4":
            query["fields"] = "core,basic,io,metadata"
            query["expandMetadata"] = "usage,timing"
            endpoint = "/api/public/v2/observations"
        else:
            endpoint = "/api/public/observations"
        rows = client.request("GET", endpoint + "?" + urlencode(query)).get("data", [])
        row = next((x for x in rows if x["id"] == item["span_id"]), None)
        if row is None or decode_io(row.get("output")) != decode_io(expected_output):
            failures.append({"record_id": item["record_id"], "reason": "missing observation or output mismatch"})
        if row is not None:
            for field, source_field in (("startTime", "startTimeUnixNano"), ("endTime", "endTimeUnixNano")):
                # Langfuse read APIs expose millisecond timestamps.
                if not row.get(field) or int(nanos(row[field])) // 10**6 != int(span[source_field]) // 10**6:
                    failures.append({"record_id": item["record_id"], "reason": f"timestamp mismatch: {field}"})
            metadata = row.get("metadata") or {}
            expected_metadata = {
                "reward": item["reward"], "import_batch": report["import_batch"],
            }
            for field in ("usage", "timing"):
                encoded = next(a["value"]["stringValue"] for a in span["attributes"]
                               if a["key"] == f"langfuse.trace.metadata.{field}")
                expected_metadata[field] = json.loads(encoded)
            for field in ("verifier_passed", "verifier_failed"):
                value = next((a["value"] for a in span["attributes"]
                              if a["key"] == f"langfuse.trace.metadata.{field}"), None)
                if value is not None:
                    expected_metadata[field] = int(value["intValue"]) if "intValue" in value else None
            for field, expected in expected_metadata.items():
                if field not in metadata or metadata[field] != expected:
                    failures.append({"record_id": item["record_id"], "reason": f"metadata mismatch: {field}"})
        # Read score values as well as observations; an accepted write is asynchronous.
        if item["scores"]:
            endpoint = "/api/public/v3/scores" if args.api_version == "v4" else "/api/public/v2/scores"
            scores = client.request("GET", endpoint + "?" + urlencode({"traceId": item["trace_id"], "limit": 100})).get("data", [])
        for expected in item["scores"]:
            actual = next((x for x in scores if x["id"] == expected["id"]), None)
            if actual is None or actual.get("value") != expected["value"]:
                failures.append({"record_id": item["record_id"], "reason": f"missing/mismatched score {expected['name']}"})
        if index % 10 == 0 or index == len(items):
            print(f"Verified {index}/{len(items)} traces", flush=True)
    result = {"import_batch": report["import_batch"], "checked_at": datetime.now(timezone.utc).isoformat(),
              "checked_traces": len(items), "checked_scores": sum(len(x["scores"]) for x in items),
              "failures": failures}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(dump(result) + "\n", encoding="utf-8")
    print(f"Verification failures: {len(failures)}; report: {args.report}")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="Build a bundle directly from cleaned dataset JSONL; no network")
    prep.add_argument("--dataset", type=Path, default=DATASET, help="Cleaned trajectory dataset root")
    prep.add_argument("--products", nargs="+", choices=PRODUCTS, default=list(PRODUCTS))
    prep.add_argument("--output", type=Path, required=True)
    imp = sub.add_parser("import", help="Upload prepared bundle; retries reuse the bundle's IDs")
    imp.add_argument("--bundle", type=Path, required=True)
    check = sub.add_parser("verify", help="Read back all observations and scores")
    check.add_argument("--bundle", type=Path, required=True)
    check.add_argument("--api-version", choices=["v3", "v4"], default="v4")
    check.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        return {"prepare": prepare, "import": upload, "verify": verify}[args.command](args) or 0
    except (ValueError, OSError, RuntimeError) as exc:
        parser.exit(1, f"Error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
