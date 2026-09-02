from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from astra.runners.toolathlon_dsh.dsh_adapter import (
    DSH_NODE_VERSION,
    DSH_VERSION,
    DshRuntime,
    _last_assistant_error,
    _last_assistant_text,
    _normalize_events,
    _profile_patch,
    run_dsh,
)
from astra.runners.toolathlon_dsh.lifecycle import (
    DSH_RUNTIME_GENERATED_STATE_PATHS,
    FILTER_LOW_SELLING_PRODUCTS_PREPROCESS,
    DshSingleTaskLifecycle,
    _verify_frozen_credential,
)
from astra.runners.toolathlon_dsh.orchestrator import (
    _add_dsh_tool_names,
    _dsh_name,
)
from astra.runners.toolathlon_verified.contract import ContractError
from astra.runners.toolathlon_verified.lifecycle import SingleTaskLifecycle, TASK_IMAGE
from astra.runners.toolathlon_verified.process_control import ProcessResult
from astra.runners.toolathlon_verified.trajectory import normalize_product_events


class DshRunnerTests(unittest.TestCase):
    def test_profile_uses_bounded_transport_retry_backoff(self) -> None:
        patch_text = _profile_patch()
        self.assertIn("maxRetries: 5", patch_text)
        self.assertIn("initialDelayMs: 1000", patch_text)
        self.assertIn("maxDelayMs: 16000", patch_text)

    def test_runtime_generated_state_allowlist_is_narrow(self) -> None:
        self.assertIn(
            "deployment/canvas/configs/canvas_admin_tokens.txt",
            DSH_RUNTIME_GENERATED_STATE_PATHS,
        )
        self.assertNotIn(
            "configs/google_credentials.json",
            DSH_RUNTIME_GENERATED_STATE_PATHS,
        )

    def test_empty_lock_fingerprint_does_not_require_file_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "notion_official_refresh.lock"
            lock.touch()
            with patch(
                "astra.runners.toolathlon_dsh.lifecycle.sha256_file",
                side_effect=PermissionError("lock is intentionally unreadable"),
            ) as fingerprint:
                _verify_frozen_credential(
                    lock,
                    {
                        "size_bytes": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                    },
                )
            fingerprint.assert_not_called()

    def test_filter_low_selling_overlay_only_changes_container_copy(self) -> None:
        lifecycle = object.__new__(DshSingleTaskLifecycle)
        lifecycle.args = argparse.Namespace(task_id="filter-low-selling-products")
        lifecycle.container_name = "task-container"
        lifecycle.lifecycle = unittest.mock.Mock()
        with patch.object(SingleTaskLifecycle, "_copy_tree") as copy_tree, patch.object(
            lifecycle, "_docker"
        ) as docker:
            lifecycle._copy_tree()

        copy_tree.assert_called_once_with()
        docker.assert_called_once()
        self.assertEqual(
            docker.call_args.args[:4],
            ("exec", "task-container", "python3", "-c"),
        )
        self.assertEqual(
            docker.call_args.args[5],
            FILTER_LOW_SELLING_PRODUCTS_PREPROCESS,
        )
        self.assertIn("batch_size=10", docker.call_args.args[6])
        lifecycle.lifecycle.append.assert_called_once_with(
            "preprocess.overlay_applied",
            policy="toolathlon.dsh-filter-low-selling-products-batch-size.v1",
            scope="task_container_copy_only",
            batch_size=10,
            frozen_toolathlon_source_modified=False,
        )

    def test_dsh_name_matches_harness_and_preserves_lossy_identity(self) -> None:
        self.assertEqual(
            _dsh_name("server-tool.name"),
            "mcp__toolathlon__server-tool_name_a1145dd38b4d",
        )
        raw = "tool." + ("x" * 90)
        normalized = "mcp__toolathlon__" + raw.replace(".", "_")
        digest = hashlib.sha256(f"toolathlon\0{raw}".encode()).hexdigest()[:12]
        self.assertEqual(_dsh_name(raw), f"{normalized[:51]}_{digest}")
        self.assertNotEqual(_dsh_name("a.b"), _dsh_name("a/b"))

    def test_manifest_adds_dsh_names_and_rejects_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "tools.json"
            manifest = {
                "tools": [
                    {"gateway_tool_name": "a-b"},
                    {"gateway_tool_name": "a.b"},
                ]
            }
            names = _add_dsh_tool_names(manifest, destination)
            self.assertEqual(
                names,
                ["mcp__toolathlon__a-b", "mcp__toolathlon__a_b_7696dce65892"],
            )
            self.assertEqual(manifest["dsh_run_qualification"], "go")
            self.assertEqual(
                json.loads(destination.read_text(encoding="utf-8"))["tools"][1][
                    "dsh_model_visible_tool_name"
                ],
                "mcp__toolathlon__a_b_7696dce65892",
            )
            with self.assertRaises(ContractError):
                _add_dsh_tool_names(
                    {
                        "tools": [
                            {"gateway_tool_name": "a.b"},
                            {"gateway_tool_name": "a.b"},
                        ]
                    },
                    destination,
                )

    def test_session_events_are_normalized_for_shared_trajectory(self) -> None:
        rows = [
            {
                "type": "tool/call",
                "time": 1,
                "data": {
                    "callId": "call-1",
                    "name": "mcp__toolathlon__lookup",
                    "arguments": '{"q":"x"}',
                },
            },
            {
                "type": "tool/result",
                "time": 2,
                "data": {
                    "callId": "call-1",
                    "message": {
                        "source": {"callId": "call-1"},
                        "content": [{"type": "text", "text": "ok"}],
                    },
                },
            },
            {
                "type": "assistant/message",
                "data": {
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "done"}],
                    }
                },
            },
            {"type": "turn/end", "data": {"reason": {"kind": "completed"}}},
        ]
        normalized = _normalize_events(rows)
        self.assertEqual(normalized[0]["event"], "tool.execution_start")
        self.assertEqual(normalized[1]["event"], "tool.execution_end")
        self.assertTrue(normalized[1]["success"])
        self.assertEqual(_last_assistant_text(rows), "done")
        self.assertIsNone(_last_assistant_error(rows))
        self.assertEqual(
            _last_assistant_error(
                [
                    {
                        "type": "assistant/message",
                        "data": {
                            "message": {
                                "stopReason": "error",
                                "errorMessage": "provider failed",
                            }
                        },
                    }
                ]
            ),
            "provider failed",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            summary = normalize_product_events(
                normalized,
                run_id="run",
                system_id="dsh",
                trajectory_path=root / "trajectory.jsonl",
                tool_calls_path=root / "tool-calls.jsonl",
            )
            self.assertEqual(summary["tool_started_events"], 1)
            self.assertEqual(summary["tool_terminal_events"], 1)
            self.assertEqual(summary["tool_failed_events"], 0)

    def test_adapter_runs_headless_session_in_sidecar_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dsh_root = root / "dsh"
            (dsh_root / "apps/cli/lib").mkdir(parents=True)
            (dsh_root / "packages/bundle/headless/lib").mkdir(parents=True)
            (dsh_root / "apps/cli/lib/bin.js").write_text("cli\n", encoding="utf-8")
            (dsh_root / "packages/bundle/headless/lib/index.js").write_text(
                "headless\n", encoding="utf-8"
            )
            (dsh_root / "apps/cli/package.json").write_text(
                json.dumps({"version": DSH_VERSION}), encoding="utf-8"
            )
            node = root / "node"
            node.write_text("#!/bin/sh\n", encoding="utf-8")
            node.chmod(0o755)
            bridge = root / "bridge.mjs"
            bridge.write_text("console.error('bridge')\n", encoding="utf-8")
            workspace = root / "workspace"
            output = root / "output"
            workspace.mkdir()
            output.mkdir()
            runtime = DshRuntime(node=node, root=dsh_root, bridge=bridge)
            captured_argv: list[str] = []

            def fake_monitored_process(argv: object, **kwargs: object) -> ProcessResult:
                captured_argv.extend(argv)  # type: ignore[arg-type]
                mount_specs = [
                    captured_argv[index + 1]
                    for index, value in enumerate(captured_argv[:-1])
                    if value == "--mount"
                ]
                state_mount = next(
                    value for value in mount_specs if "dst=/run/dsh-state" in value
                )
                state_source = Path(
                    state_mount.split("src=", 1)[1].split(",dst=", 1)[0]
                )
                session = state_source / "home/sessions/session-1/session.jsonl"
                session.parent.mkdir(parents=True)
                session.write_text(
                    "\n".join(
                        [
                            json.dumps(
                                {
                                    "type": "tool/call",
                                    "time": 1,
                                    "data": {
                                        "callId": "call-1",
                                        "name": "mcp__toolathlon__lookup",
                                        "arguments": "{}",
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "type": "tool/result",
                                    "time": 2,
                                    "data": {
                                        "callId": "call-1",
                                        "message": {
                                            "content": [{"type": "text", "text": "ok"}]
                                        },
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "type": "assistant/message",
                                    "data": {
                                        "message": {
                                            "content": [{"type": "text", "text": "done"}]
                                        }
                                    },
                                }
                            ),
                            json.dumps(
                                {
                                    "type": "turn/end",
                                    "data": {"reason": {"kind": "completed"}},
                                }
                            ),
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                Path(kwargs["stdout_path"]).write_text("done\n", encoding="utf-8")  # type: ignore[arg-type]
                Path(kwargs["stderr_path"]).write_text("", encoding="utf-8")  # type: ignore[arg-type]
                self.assertNotIn("DEEPSEEK_API_KEY", kwargs["env"])  # type: ignore[operator]
                return ProcessResult(0, 0.1, "product_exit", 1234, False)

            with patch(
                "astra.runners.toolathlon_dsh.dsh_adapter.run_monitored_process",
                side_effect=fake_monitored_process,
            ), patch(
                "astra.runners.toolathlon_dsh.dsh_adapter._remove_dsh_container",
                return_value="already_absent",
            ) as cleanup:
                outcome = run_dsh(
                    runtime=runtime,
                    public_bundle={
                        "prompt": {"system": "system instruction", "task": "task"}
                    },
                    gateway_url="http://127.0.0.1:1234/sse",
                    workspace=workspace,
                    output_dir=output,
                    proxy_url="http://127.0.0.1:4321/v1",
                    deadline_seconds=30,
                    budget_exceeded=lambda: False,
                    model_request_snapshot=lambda: {
                        "provider_requests_forwarded": 0
                    },
                    task_mcp_tool_names=["mcp__toolathlon__lookup"],
                )
            cleanup.assert_called_once()
            self.assertEqual(outcome.terminal_status, "completed")
            self.assertEqual(outcome.output, "done")
            self.assertTrue((output / "dsh-session.jsonl").is_file())
            self.assertEqual(captured_argv[:2], ["/usr/bin/docker", "run"])
            user_index = captured_argv.index("--user")
            self.assertEqual(
                captured_argv[user_index + 1],
                f"{os.getuid()}:{os.getgid()}",
            )
            self.assertIn("--read-only", captured_argv)
            self.assertIn("no-new-privileges", captured_argv)
            self.assertIn("DSH_TOOLS_MODE=native", captured_argv)
            self.assertIn("NARB_DISABLE_NATIVE_CACHE=1", captured_argv)
            self.assertEqual(
                sum(value == "--mount" for value in captured_argv),
                5,
            )
            self.assertIn(TASK_IMAGE, captured_argv)

    def test_dsh_slot_config_marks_evaluator_and_permission_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            permission = root / "permission.json"
            permission.write_text(json.dumps({"products": {"astra": {}}}), encoding="utf-8")
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "run": {
                            "permission_policy": str(permission),
                            "system_id": "astra",
                        },
                        "evaluator": {"command": ["container-eval"]},
                    }
                ),
                encoding="utf-8",
            )
            lifecycle = object.__new__(DshSingleTaskLifecycle)
            lifecycle.credential_manifest_path = root / "manifest.json"
            lifecycle.dsh_runtime_generated_paths = [
                "deployment/canvas/configs/canvas_admin_tokens.txt"
            ]
            with patch.object(
                SingleTaskLifecycle, "_write_slot_config", return_value=config_path
            ):
                lifecycle._write_slot_config(12345)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["run"]["system_id"], "dsh")
            self.assertEqual(config["benchmark_status"], "exploratory_dsh_only")
            self.assertEqual(
                config["dsh_runtime"]["runtime_generated_state_paths"],
                ["deployment/canvas/configs/canvas_admin_tokens.txt"],
            )
            self.assertIn(
                "--evaluate_regardless_of_agent_status",
                config["evaluator"]["command"],
            )
            updated_permission = json.loads(permission.read_text(encoding="utf-8"))
            self.assertEqual(
                updated_permission["products"]["dsh"]["unresolved_action"],
                "deny",
            )


if __name__ == "__main__":
    unittest.main()
