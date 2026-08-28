from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

from astra.runners.dsh_terminal_bench.driver import (
    ProtocolError,
    run,
    summarize_events,
)


class DriverSummaryTests(unittest.TestCase):
    def test_summarizes_native_usage_without_double_counting_cache(self) -> None:
        events = [
            {"type": "turn/start", "data": {}},
            {"type": "step/start", "data": {}},
            {"type": "tool/call", "data": {}},
            {
                "type": "assistant/message",
                "data": {
                    "content": [{"type": "text", "text": "done"}],
                    "usage": {
                        "inputTokens": 10,
                        "cacheReadTokens": 4,
                        "cacheWriteTokens": 2,
                        "outputTokens": 3,
                        "reasoningTokens": 1,
                    },
                },
            },
            {
                "type": "turn/end",
                "data": {"reason": {"kind": "completed"}},
            },
        ]
        summary = summarize_events(events, "session-1")
        self.assertEqual(summary["final_response"], "done")
        self.assertEqual(summary["finish_reason"], "completed")
        self.assertEqual(summary["tool_call_count"], 1)
        self.assertEqual(
            summary["usage"],
            {
                "fresh_input_tokens": 10,
                "cache_read_tokens": 4,
                "cache_write_tokens": 2,
                "output_tokens": 3,
                "reasoning_tokens": 1,
            },
        )

    def test_missing_usage_stays_null(self) -> None:
        summary = summarize_events([], "session-1")
        self.assertIsNone(summary["usage"])

    def test_classifies_profile_step_limit_as_max_turns(self) -> None:
        events = [
            {"type": "step/start", "data": {}}
            for _ in range(50)
        ] + [
            {"type": "turn/end", "data": {"reason": {"kind": "blocked"}}}
        ]
        summary = summarize_events(events, "session-1", max_turns=50)
        self.assertEqual(summary["step_count"], 50)
        self.assertEqual(summary["finish_reason"], "max_turns")

    def test_rejects_invalid_usage(self) -> None:
        with self.assertRaisesRegex(ProtocolError, "inputTokens"):
            summarize_events(
                [
                    {
                        "type": "assistant/message",
                        "data": {"usage": {"inputTokens": -1}},
                    }
                ],
                "session-1",
            )

    def test_jsonrpc_driver_waits_for_receipt_and_root_idle(self) -> None:
        fake_runtime = r'''#!/usr/bin/env python3
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "session/prompt":
        session_id = message["params"]["sessionId"]
        print(json.dumps({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": session_id, "event": {"type": "agent/inbox/spliced", "data": {"inserted": [{"id": "message-1"}]}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {"messageId": "message-1"}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": session_id, "event": {"type": "assistant/message", "data": {"message": {"content": [{"type": "text", "text": "ok"}]}, "usage": {"inputTokens": 5, "cacheReadTokens": 2, "cacheWriteTokens": 0, "outputTokens": 1, "reasoningTokens": 0}}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "session.event", "params": {"sessionId": session_id, "event": {"type": "turn/end", "data": {"reason": {"kind": "completed"}}}}}), flush=True)
        print(json.dumps({"jsonrpc": "2.0", "method": "session.status", "params": {"sessionId": session_id, "status": "idle"}}), flush=True)
    elif message.get("method") == "shutdown":
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": {}}), flush=True)
        break
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "runtime"
            runtime.write_text(fake_runtime, encoding="utf-8")
            runtime.chmod(0o755)
            config = root / "config.yml"
            config.write_text("[]\n", encoding="utf-8")
            instruction = root / "instruction.md"
            instruction.write_text("fix it", encoding="utf-8")
            args = argparse.Namespace(
                runtime=runtime,
                config=config,
                instruction_file=instruction,
                result_file=root / "result.json",
                events_file=root / "events.jsonl",
                runtime_stderr_file=root / "runtime.stderr",
                session_root=root / "sessions",
                session_id="root-session",
                cwd=root,
                provider="deepseek-official",
                model="deepseek-v4-flash",
                max_tokens=49152,
                max_turns=None,
                timeout_sec=5,
                initialize_only=False,
            )
            result = run(args)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["final_response"], "ok")
            self.assertEqual(result["usage"]["cache_read_tokens"], 2)
            rows = [
                json.loads(line)
                for line in args.events_file.read_text(encoding="utf-8").splitlines()
            ]
            self.assertTrue(any(row.get("method") == "session.status" for row in rows))


if __name__ == "__main__":
    unittest.main()
