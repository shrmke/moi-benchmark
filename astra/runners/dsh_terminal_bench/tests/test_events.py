from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from astra.runners.dsh_terminal_bench.events import validate_event_stream


class EventValidationTests(unittest.TestCase):
    def test_validates_root_session_and_ignores_other_sessions(self) -> None:
        rows = [
            {
                "method": "session.event",
                "params": {
                    "sessionId": "other",
                    "event": {"type": "assistant/message", "data": {}},
                },
            },
            {
                "method": "session.event",
                "params": {
                    "sessionId": "root",
                    "event": {
                        "type": "turn/end",
                        "data": {"reason": {"kind": "completed"}},
                    },
                },
            },
            {
                "method": "session.status",
                "params": {"sessionId": "root", "status": "idle"},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            summary = validate_event_stream(path, session_id="root")
            self.assertEqual(summary["event_count"], 1)
            self.assertEqual(summary["finish_reason"], "completed")
            self.assertEqual(summary["wire_message_count"], 3)

    def test_rejects_stream_without_root_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text('{}\n', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "idle"):
                validate_event_stream(path, session_id="root")


if __name__ == "__main__":
    unittest.main()
