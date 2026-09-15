from argparse import Namespace
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import langfuse_import as importer


class ToolathlonLangfuseImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = self.root / "dataset"
        (self.dataset / "data/complete").mkdir(parents=True)
        (self.dataset / "data/partial").mkdir(parents=True)
        self.record = {
            "record_id": "astra/sample/run-1",
            "agent": {"name": "astra", "model": "deepseek-v4-flash", "commit": "969550b"},
            "benchmark": {"name": "Toolathlon", "task_id": "sample", "selection_kind": "native"},
            "trial": {
                "attempt_run_id": "run-1",
                "started_at": "2026-09-01T00:00:00Z",
                "finished_at": "2026-09-01T00:00:01Z",
                "terminal_status": "completed",
                "segments": [],
            },
            "instruction": "Complete the sample task",
            "messages": [
                {
                    "seq": 0,
                    "role": "user",
                    "content": "Complete the sample task",
                    "reasoning_content": None,
                    "timestamp": "2026-09-01T00:00:00Z",
                    "tool_calls": [],
                    "tool_call_id": None,
                    "tool_name": None,
                    "is_error": None,
                    "segment_index": 0,
                    "internal_content_omitted": False,
                },
                {
                    "seq": 1,
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "inspect the file",
                    "timestamp": "2026-09-01T00:00:00.2Z",
                    "tool_calls": [
                        {"id": "call-1", "name": "read", "arguments_json": '{"path":"a"}'}
                    ],
                    "tool_call_id": None,
                    "tool_name": None,
                    "is_error": None,
                    "segment_index": 1,
                    "internal_content_omitted": False,
                },
                {
                    "seq": 2,
                    "role": "tool",
                    "content": "ok",
                    "reasoning_content": None,
                    "timestamp": "2026-09-01T00:00:00.4Z",
                    "tool_calls": [],
                    "tool_call_id": "call-1",
                    "tool_name": "read",
                    "is_error": False,
                    "segment_index": 1,
                    "internal_content_omitted": False,
                },
            ],
            "outcome": {
                "status": "pass",
                "evaluator_valid": True,
                "evaluator_pass": True,
                "evaluation_kind": "original",
            },
            "usage": {
                "input_tokens": None,
                "output_tokens": 10,
                "total_tokens": None,
                "coverage": {"status": "partial"},
                "tool_call_count": 1,
            },
            "timing": {"agent_seconds": 1.0, "evaluator_seconds": None},
            "quality": {
                "tier": "complete",
                "reasons": [],
                "reasoning_included": True,
                "token_usage_affects_tier": False,
            },
            "source": {"attempt_path": "work/sample"},
        }
        (self.dataset / "data/complete/astra.jsonl").write_text(json.dumps(self.record) + "\n")
        (self.dataset / "data/partial/astra.jsonl").write_text("")

    def test_prepare_preserves_reasoning_and_missing_usage(self):
        bundle = self.root / "bundle.jsonl"
        importer.prepare(Namespace(dataset=self.dataset, output=bundle))
        report, items = importer.read_bundle(bundle)
        self.assertEqual(report["traces"], 1)
        item = items[0]
        span = item["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attributes = {attribute["key"]: attribute["value"] for attribute in span["attributes"]}
        output = json.loads(attributes["langfuse.observation.output"]["stringValue"])
        self.assertEqual(output[1]["reasoning_content"], "inspect the file")
        self.assertIn("usage.input_tokens", item["metadata"]["missing_fields"])
        self.assertEqual(item["reward"], 1)

    def test_ids_are_stable_for_one_bundle(self):
        first = importer.make_item(self.record, "batch")
        second = importer.make_item(self.record, "batch")
        self.assertEqual(first["trace_id"], second["trace_id"])
        self.assertEqual(first["span_id"], second["span_id"])
        self.assertEqual(first["scores"][0]["id"], second["scores"][0]["id"])

    def test_invalid_evaluator_is_rejected(self):
        self.record["outcome"]["evaluator_valid"] = False
        (self.dataset / "data/complete/astra.jsonl").write_text(json.dumps(self.record) + "\n")
        with self.assertRaisesRegex(ValueError, "evaluator is not valid"):
            importer.read_dataset(self.dataset)

    def test_import_reuses_bundle_and_verify_checks_output_metadata_and_score(self):
        bundle = self.root / "bundle.jsonl"
        importer.prepare(Namespace(dataset=self.dataset, output=bundle))
        report, items = importer.read_bundle(bundle)
        item = items[0]
        span = item["payload"]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        expected_output = next(
            attribute["value"]["stringValue"]
            for attribute in span["attributes"]
            if attribute["key"] == "langfuse.observation.output"
        )
        calls = []

        def request(method, path, body=None):
            calls.append((method, path, copy.deepcopy(body)))
            if method == "POST":
                return {}
            if "observations?" in path:
                return {
                    "data": [
                        {
                            "id": item["span_id"],
                            "output": json.loads(expected_output),
                            "startTime": self.record["trial"]["started_at"],
                            "endTime": self.record["trial"]["finished_at"],
                            "metadata": item["metadata"],
                        }
                    ]
                }
            return {"data": item["scores"]}

        with patch.object(importer, "Client") as client:
            client.return_value.request.side_effect = request
            importer.upload(Namespace(bundle=bundle))
            first_upload = copy.deepcopy(calls)
            calls.clear()
            importer.upload(Namespace(bundle=bundle))
            self.assertEqual(first_upload, calls)
            result = importer.verify(
                Namespace(
                    bundle=bundle,
                    api_version="v4",
                    report=self.root / "verification.json",
                )
            )
            self.assertEqual(result, 0)
            self.assertEqual(report["import_batch"], item["metadata"]["import_batch"])


if __name__ == "__main__":
    unittest.main()
