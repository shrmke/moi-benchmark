import json
import tempfile
import unittest
import urllib.error
import uuid
from pathlib import Path
from unittest.mock import patch

from astra.runners.astra_terminal_bench.trajectory_export import (
    discover_session_id,
    export_trajectory,
    register_session,
    validate_trajectory_bundle,
)


class TrajectoryExportTests(unittest.TestCase):
    def _fixture(self, root: Path, session_id: str):
        credentials = root / "credentials"
        credentials.mkdir()
        (credentials / "credentials.json").write_text(
            json.dumps(
                {
                    "current_profile": "default",
                    "profiles": {"default": {"access_token": "secret"}},
                }
            ),
            encoding="utf-8",
        )
        session_dir = (
            root
            / "sessions"
            / "v1"
            / "users"
            / "b64-user"
            / "sessions"
            / session_id
        )
        session_dir.parent.mkdir(parents=True, exist_ok=True)
        (session_dir.parent / f"{session_id}.jsonl").write_text(
            "\n".join(
                (
                    json.dumps(
                        {
                            "type": "session_start",
                            "session_id": session_id,
                        }
                    ),
                    json.dumps(
                        {
                            "type": "turn",
                            "session_id": session_id,
                        }
                    ),
                    json.dumps(
                        {
                            "type": "session_end",
                            "session_id": session_id,
                        }
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        (session_dir / "tool-results").mkdir(parents=True)
        (session_dir / "step_events.jsonl").write_text(
            '{"event_type":"StepStarted"}\n',
            encoding="utf-8",
        )
        (session_dir / "tool-results" / "call.txt").write_text(
            "tool output",
            encoding="utf-8",
        )
        return credentials, root / "sessions"

    def test_registration_uses_server_assigned_session_id(self):
        controller_run_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials, _sessions = self._fixture(root, str(uuid.uuid4()))
            with patch(
                "astra.runners.astra_terminal_bench.trajectory_export._post_json",
                return_value={"session_id": session_id, "status": "active"},
            ) as post:
                value = register_session(
                    api_url="http://example.invalid",
                    credentials_dir=credentials,
                    controller_run_id=controller_run_id,
                    task_id="modernize-scientific-stack",
                )

            self.assertEqual(value["session_id"], session_id)
            body = post.call_args.args[3]
            self.assertEqual(
                body["metadata"]["controller_run_id"],
                controller_run_id,
            )
            self.assertEqual(body["metadata"]["condition"], "C0")
            self.assertIs(body["metadata"]["full_llm_capture"], True)

    def test_discovers_only_session_in_isolated_home(self):
        session_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            sessions = Path(directory)
            session_dir = sessions / "v1" / "users" / "owner" / "sessions"
            session_dir.mkdir(parents=True)
            (session_dir / f"{session_id}.jsonl").write_text("{}\n")

            self.assertEqual(discover_session_id(sessions), session_id)

    def test_exports_server_and_local_session_trajectory(self):
        session_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials, sessions = self._fixture(root, session_id)

            def fake_get(_api_url, _token, path, query=None):
                if path.startswith("/sessions/"):
                    return {"session_id": session_id, "status": "open"}
                if path.startswith("/events/session/"):
                    return {
                        "events": [{"event_id": "event-1", "content": "short"}],
                        "total": 1,
                        "next_cursor": None,
                    }
                if path == "/events/event-1":
                    return {
                        "event_id": "event-1",
                        "session_id": session_id,
                        "event_type": "turn",
                        "content": "complete event content",
                    }
                raise AssertionError(path)

            output = root / "output"
            with patch(
                "astra.runners.astra_terminal_bench.trajectory_export._get_json",
                side_effect=fake_get,
            ):
                result = export_trajectory(
                    session_id=session_id,
                    terminal_status="completed",
                    sessions_root=sessions,
                    output_root=output,
                    api_url="http://example.invalid",
                    credentials_dir=credentials,
                )

            self.assertEqual(result, 0)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["capture_status"], "complete")
            self.assertFalse(manifest["failed"])
            self.assertEqual(manifest["server_event_count"], 1)
            self.assertEqual(manifest["local_file_count"], 3)
            self.assertEqual(manifest["local_trace_file_count"], 2)
            self.assertEqual(manifest["tool_result_file_count"], 1)
            self.assertTrue(manifest["local_journal_saved"])
            self.assertEqual(manifest["local_journal_event_count"], 3)
            self.assertEqual(
                manifest["local_journal_terminal_event"],
                "session_end",
            )
            self.assertRegex(manifest["server_session_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(manifest["server_events_sha256"], r"^[0-9a-f]{64}$")
            copied_paths = {row["path"] for row in manifest["local_files"]}
            self.assertTrue(
                any(path.endswith("/step_events.jsonl") for path in copied_paths)
            )
            self.assertTrue(
                any(path.endswith("/tool-results/call.txt") for path in copied_paths)
            )
            event = json.loads((output / "server-events.jsonl").read_text())
            self.assertEqual(event["content"], "complete event content")
            validated = validate_trajectory_bundle(
                output,
                session_id=session_id,
                terminal_status="completed",
            )
            self.assertEqual(validated["server_event_count"], 1)
            tool_result = next(output.rglob("tool-results/call.txt"))
            tool_result.write_text("tampered", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "hash"):
                validate_trajectory_bundle(
                    output,
                    session_id=session_id,
                    terminal_status="completed",
                )

    def test_api_failure_is_visible_but_local_failure_trace_survives(self):
        session_id = str(uuid.uuid4())
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            credentials, sessions = self._fixture(root, session_id)
            output = root / "output"
            with patch(
                "astra.runners.astra_terminal_bench.trajectory_export._get_json",
                side_effect=urllib.error.URLError("offline"),
            ):
                result = export_trajectory(
                    session_id=session_id,
                    terminal_status="timeout",
                    sessions_root=sessions,
                    output_root=output,
                    api_url="http://example.invalid",
                    credentials_dir=credentials,
                )

            self.assertEqual(result, 3)
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(manifest["capture_status"], "partial")
            self.assertTrue(manifest["failed"])
            self.assertEqual(manifest["product_terminal_status"], "timeout")
            self.assertEqual(manifest["errors"][0]["source"], "server_api")

    def test_empty_or_cross_session_evidence_is_not_complete(self):
        for scenario in (
            "empty_local_journal",
            "no_agent_activity",
            "zero_server_events",
            "server_total_mismatch",
            "wrong_server_session",
            "wrong_event_session",
        ):
            with self.subTest(scenario=scenario):
                session_id = str(uuid.uuid4())
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    credentials, sessions = self._fixture(root, session_id)
                    if scenario == "empty_local_journal":
                        next(sessions.rglob(f"{session_id}.jsonl")).write_text(
                            "",
                            encoding="utf-8",
                        )
                    elif scenario == "no_agent_activity":
                        next(sessions.rglob(f"{session_id}.jsonl")).write_text(
                            "\n".join(
                                (
                                    json.dumps(
                                        {
                                            "type": "session_start",
                                            "session_id": session_id,
                                        }
                                    ),
                                    json.dumps(
                                        {
                                            "type": "session_end",
                                            "session_id": session_id,
                                        }
                                    ),
                                )
                            )
                            + "\n",
                            encoding="utf-8",
                        )

                    def fake_get(_api_url, _token, path, query=None):
                        if path.startswith("/sessions/"):
                            return {
                                "session_id": (
                                    str(uuid.uuid4())
                                    if scenario == "wrong_server_session"
                                    else session_id
                                ),
                                "status": "open",
                            }
                        if path.startswith("/events/session/"):
                            return {
                                "events": (
                                    []
                                    if scenario == "zero_server_events"
                                    else [{"event_id": "event-1"}]
                                ),
                                "total": (
                                    0
                                    if scenario == "zero_server_events"
                                    else (
                                        2
                                        if scenario
                                        == "server_total_mismatch"
                                        else 1
                                    )
                                ),
                                "next_cursor": None,
                            }
                        if path == "/events/event-1":
                            return {
                                "event_id": "event-1",
                                "session_id": (
                                    str(uuid.uuid4())
                                    if scenario == "wrong_event_session"
                                    else session_id
                                ),
                                "event_type": "turn",
                                "content": "complete event content",
                            }
                        raise AssertionError(path)

                    output = root / "output"
                    with patch(
                        "astra.runners.astra_terminal_bench."
                        "trajectory_export._get_json",
                        side_effect=fake_get,
                    ):
                        result = export_trajectory(
                            session_id=session_id,
                            terminal_status="completed",
                            sessions_root=sessions,
                            output_root=output,
                            api_url="http://example.invalid",
                            credentials_dir=credentials,
                        )

                    manifest = json.loads(
                        (output / "manifest.json").read_text()
                    )
                    self.assertEqual(result, 3)
                    self.assertEqual(manifest["capture_status"], "partial")


if __name__ == "__main__":
    unittest.main()
