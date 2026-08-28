from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from astra.runners.lifecycle_c0.audit import (
    AuditError,
    _validate_dsh_trajectory,
)


class DshAuditIntegrationTests(unittest.TestCase):
    def test_audits_saved_trajectory_and_rejects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory) / "trial"
            agent_dir = trial / "agent"
            agent_dir.mkdir(parents=True)
            event_rows = [
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
            event_path = agent_dir / "dsh-events.jsonl"
            event_path.write_text(
                "".join(json.dumps(row) + "\n" for row in event_rows),
                encoding="utf-8",
            )
            trajectory_sha256 = hashlib.sha256(event_path.read_bytes()).hexdigest()
            (agent_dir / "dsh-run.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "completed",
                        "session_id": "root",
                        "event_count": 1,
                        "finish_reason": "completed",
                    }
                ),
                encoding="utf-8",
            )
            metadata = {
                "trajectory_capture_blocking": False,
                "trajectory_capture_path": "agent/dsh-events.jsonl",
                "dsh_trajectory_status": "saved",
                "dsh_trajectory_sha256": trajectory_sha256,
                "dsh_session_id": "root",
                "dsh_event_count": 1,
                "dsh_finish_reason": "completed",
            }
            started = {
                "product": "dsh",
                "trajectory_capture_required": True,
                "trajectory_capture_mode": "dsh_jsonrpc_event_stream",
                "trajectory_capture_blocking": False,
            }
            completed = {
                "trajectory_capture_blocking": False,
                "dsh_trajectory_status": "saved",
                "dsh_trajectory_sha256": trajectory_sha256,
                "dsh_session_id": "root",
                "dsh_event_count": 1,
                "dsh_finish_reason": "completed",
            }
            result = _validate_dsh_trajectory(
                trial / "result.json", metadata, started, completed
            )
            self.assertEqual(result["status"], "saved")
            event_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaises(AuditError):
                _validate_dsh_trajectory(
                    trial / "result.json", metadata, started, completed
                )

    def test_audits_terminalbench_max_turns_normalization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            trial = Path(directory) / "trial"
            agent_dir = trial / "agent"
            agent_dir.mkdir(parents=True)
            event_rows = [
                {
                    "method": "session.event",
                    "params": {
                        "sessionId": "root",
                        "event": {"type": "step/start", "data": {}},
                    },
                }
                for _ in range(50)
            ]
            event_rows.extend(
                [
                    {
                        "method": "session.event",
                        "params": {
                            "sessionId": "root",
                            "event": {
                                "type": "turn/end",
                                "data": {"reason": {"kind": "blocked"}},
                            },
                        },
                    },
                    {
                        "method": "session.status",
                        "params": {"sessionId": "root", "status": "idle"},
                    },
                ]
            )
            event_path = agent_dir / "dsh-events.jsonl"
            event_path.write_text(
                "".join(json.dumps(row) + "\n" for row in event_rows),
                encoding="utf-8",
            )
            trajectory_sha256 = hashlib.sha256(event_path.read_bytes()).hexdigest()
            (agent_dir / "dsh-run.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "completed",
                        "session_id": "root",
                        "event_count": 51,
                        "finish_reason": "max_turns",
                    }
                ),
                encoding="utf-8",
            )
            metadata = {
                "trajectory_capture_blocking": False,
                "trajectory_capture_path": "agent/dsh-events.jsonl",
                "dsh_trajectory_status": "saved",
                "dsh_trajectory_sha256": trajectory_sha256,
                "dsh_session_id": "root",
                "dsh_event_count": 51,
                "dsh_finish_reason": "max_turns",
                "dsh_max_turns": 50,
            }
            started = {
                "product": "dsh",
                "trajectory_capture_required": True,
                "trajectory_capture_mode": "dsh_jsonrpc_event_stream",
                "trajectory_capture_blocking": False,
            }
            completed = {
                "trajectory_capture_blocking": False,
                "dsh_trajectory_status": "saved",
                "dsh_trajectory_sha256": trajectory_sha256,
                "dsh_session_id": "root",
                "dsh_event_count": 51,
                "dsh_finish_reason": "max_turns",
            }
            result = _validate_dsh_trajectory(
                trial / "result.json", metadata, started, completed
            )
            self.assertEqual(result["finish_reason"], "max_turns")


if __name__ == "__main__":
    unittest.main()
