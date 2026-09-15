import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import clean_astra


class CleanerTests(unittest.TestCase):
    def write_attempt(
        self,
        root: Path,
        task: str,
        events: list[dict],
        *,
        evaluator: dict | None = None,
        usage: list[dict] | None = None,
    ) -> Path:
        attempt = root / task / "attempt-1"
        (attempt / "evaluator").mkdir(parents=True)
        (attempt / "run.json").write_text(
            json.dumps(
                {
                    "run_id": f"run-{task}",
                    "task_id": task,
                    "started_at": "2026-09-01T00:00:00+00:00",
                    "finished_at": "2026-09-01T00:01:00+00:00",
                    "terminal_status": "completed",
                    "verify_status": "pass" if (evaluator or {}).get("pass") else "no_pass",
                    "trajectory": {
                        "tool_started_events": sum(
                            event["event"] == "tool_transport_started" for event in events
                        ),
                        "tool_terminal_events": sum(
                            event["event"]
                            in {"tool_transport_completed", "tool_transport_failed"}
                            for event in events
                        ),
                    },
                }
            )
        )
        (attempt / "task-bundle.public.json").write_text(
            json.dumps({"prompt": {"task": f"Do {task}"}, "tools": {}})
        )
        (attempt / "trajectory.jsonl").write_text(
            "\n".join(json.dumps(event) for event in events) + "\n"
        )
        (attempt / "model-usage.jsonl").write_text(
            "\n".join(json.dumps(row) for row in (usage or [])) + ("\n" if usage else "")
        )
        if evaluator is not None:
            (attempt / "evaluator/eval_res.json").write_text(json.dumps(evaluator))
        return attempt

    def event(self, sequence: int, name: str, native: dict) -> dict:
        return {
            "sequence": sequence,
            "event": name,
            "timestamp": f"2026-09-01T00:00:{sequence:02d}+00:00",
            "native": native,
        }

    def write_selection(self, path: Path, rows: list[dict]) -> None:
        fields = ["task", "status", "terminal", "kind", "source", "run_id"]
        with path.open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def test_reasoning_and_tool_pair_are_retained(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = [
                self.event(1, "session_info", {"session_id": "s1", "run_id": "r1"}),
                self.event(2, "reasoning_delta", {"content": "inspect first"}),
                self.event(
                    3,
                    "tool_call",
                    {
                        "tool_call": {
                            "id": "c1",
                            "function": {"name": "read", "arguments": '{"path":"a"}'},
                        }
                    },
                ),
                self.event(4, "tool_transport_started", {"call_id": "c1"}),
                self.event(5, "tool_transport_completed", {"call_id": "c1"}),
                self.event(
                    6,
                    "tool_call_end",
                    {"call_id": "c1", "tool": "read", "result": "ok", "success": True},
                ),
                self.event(7, "text_done", {"full_text": "finished"}),
                self.event(8, "run_finished", {"run_id": "r1", "status": "completed"}),
                self.event(9, "turn_complete", {}),
            ]
            attempt = self.write_attempt(root, "sample", events, evaluator={"pass": True})
            selection = root / "results.csv"
            self.write_selection(
                selection,
                [
                    {
                        "task": "sample",
                        "status": "pass",
                        "terminal": "completed",
                        "kind": "native",
                        "source": str(attempt),
                        "run_id": "run-sample",
                    }
                ],
            )
            report = clean_astra.clean(selection, root / "output", root)
            record = json.loads((root / "output/data/complete/astra.jsonl").read_text())
            self.assertEqual(report["complete"], 1)
            self.assertEqual(record["record_id"], "astra/sample/run-sample")
            self.assertEqual(record["messages"][1]["reasoning_content"], "inspect first")
            self.assertEqual(
                record["messages"][1]["tool_calls"][0]["id"],
                record["messages"][2]["tool_call_id"],
            )

    def test_missing_token_usage_does_not_make_continuation_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = [
                self.event(1, "session_info", {"session_id": "s1", "run_id": "r1"}),
                self.event(2, "reasoning_delta", {"content": "part one"}),
                self.event(3, "run_interrupted", {"run_id": "r1", "resumable": True}),
                self.event(4, "transport_continuation", {"run_id": "r1"}),
                self.event(5, "session_info", {"session_id": "s1", "run_id": "r2"}),
                self.event(6, "text_done", {"full_text": "done"}),
                self.event(7, "run_finished", {"run_id": "r2", "status": "completed"}),
                self.event(8, "turn_complete", {}),
            ]
            usage = [
                {"event": "model_request.started", "model_request_id": "m1"},
                {
                    "event": "model_request.completed",
                    "model_request_id": "m1",
                    "success": True,
                    "token_usage": {},
                },
            ]
            attempt = self.write_attempt(
                root, "continued", events, evaluator={"pass": False}, usage=usage
            )
            selection = root / "results.csv"
            self.write_selection(
                selection,
                [
                    {
                        "task": "continued",
                        "status": "no_pass",
                        "terminal": "completed",
                        "kind": "native",
                        "source": str(attempt),
                        "run_id": "run-continued",
                    }
                ],
            )
            report = clean_astra.clean(selection, root / "output", root)
            record = json.loads((root / "output/data/complete/astra.jsonl").read_text())
            self.assertEqual(report["complete"], 1)
            self.assertEqual(record["usage"]["coverage"]["status"], "partial")
            self.assertFalse(record["quality"]["token_usage_affects_tier"])

    def test_invalid_evaluator_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = [
                self.event(1, "session_info", {"session_id": "s1", "run_id": "r1"}),
                self.event(2, "text_done", {"full_text": "done"}),
                self.event(3, "run_finished", {"run_id": "r1", "status": "completed"}),
            ]
            attempt = self.write_attempt(root, "invalid", events, evaluator=None)
            selection = root / "results.csv"
            self.write_selection(
                selection,
                [
                    {
                        "task": "invalid",
                        "status": "pass",
                        "terminal": "completed",
                        "kind": "native",
                        "source": str(attempt),
                        "run_id": "run-invalid",
                    }
                ],
            )
            report = clean_astra.clean(selection, root / "output", root)
            self.assertEqual(report["retained"], 0)
            self.assertEqual(report["excluded"], 1)


if __name__ == "__main__":
    unittest.main()
