import asyncio
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from astra.runners.astra_smoke.core import (
    CLEAN,
    PROCESS_KILL,
    ControllerConfig,
    FaultController,
    JsonlLedger,
    SmokeConfigurationError,
    astra_args,
    lifecycle_gate_passes,
    normalize_linux_arch,
    parse_astra_json,
    validate_linux_elf,
    write_minimal_credentials,
)


class Result:
    def __init__(self, return_code=0, stdout="", stderr=""):
        self.return_code = return_code
        self.stdout = stdout
        self.stderr = stderr


IDENTITY = {
    "pid": 1234,
    "ppid": 1200,
    "pgid": 1234,
    "sid": 1234,
    "start_ticks": 99,
    "exe": "/installed-agent/astra",
    "cgroup": "0::/docker/test\n",
    "supervisor": {
        "pid": 1200,
        "ppid": 1,
        "pgid": 1200,
        "sid": 1200,
        "start_ticks": 90,
        "exe": "/usr/bin/python3",
        "cgroup": "0::/docker/test\n",
    },
}


class FakeEnvironment:
    def __init__(self, condition):
        self.condition = condition
        self.commands = []

    async def exec(self, command, timeout_sec=None):
        self.commands.append(command)
        if command.startswith("cat "):
            return Result(stdout=json.dumps(IDENTITY))
        if command.startswith("test -e "):
            return Result()
        if "probe.py" in command and " kill " in f" {command} ":
            return Result(
                stdout=json.dumps(
                    {
                        "status": "killed",
                        "root_pid": 1234,
                        "supervisor_pid": 1200,
                        "freeze_rounds": 2,
                        "targeted_tree_pids": [1234, 1235],
                        "targeted_descendant_pids": [1235],
                        "targeted_pgids": [1234, 1235],
                        "targeted_sids": [1234, 1235],
                        "surviving_tree_pids": [],
                        "signal_errors": [],
                    }
                )
            )
        if command == "true":
            return Result()
        return Result(1)


class CoreTests(unittest.TestCase):
    def test_rejects_macho_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "astra"
            path.write_bytes(b"\xcf\xfa\xed\xfe" + b"\0" * 32)
            with self.assertRaises(SmokeConfigurationError):
                validate_linux_elf(path)

    def test_accepts_elf_aarch64(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "astra"
            header = bytearray(20)
            header[:6] = b"\x7fELF\x02\x01"
            header[18:20] = (183).to_bytes(2, "little")
            path.write_bytes(header)
            self.assertEqual(validate_linux_elf(path), "aarch64")

    def test_normalizes_container_arch_aliases(self):
        self.assertEqual(normalize_linux_arch("arm64\n"), "aarch64")
        self.assertEqual(normalize_linux_arch("amd64"), "x86_64")

    def test_writes_minimal_mode_0600_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            token = "unit-test-access-token"
            path = Path(directory) / "credentials.json"
            write_minimal_credentials(path, token)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["current_profile"], "default")
            self.assertEqual(
                payload["profiles"]["default"], {"access_token": token}
            )
            self.assertNotIn("refresh_token", path.read_text(encoding="utf-8"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_lifecycle_gate_rejects_no_hit_and_wrong_exit_semantics(self):
        self.assertFalse(
            lifecycle_gate_passes(
                CLEAN,
                trigger_hit=False,
                fault_injected=False,
                turn_return_code=0,
            )
        )
        self.assertTrue(
            lifecycle_gate_passes(
                CLEAN,
                trigger_hit=True,
                fault_injected=False,
                turn_return_code=0,
            )
        )
        self.assertFalse(
            lifecycle_gate_passes(
                PROCESS_KILL,
                trigger_hit=True,
                fault_injected=True,
                turn_return_code=0,
            )
        )
        self.assertTrue(
            lifecycle_gate_passes(
                PROCESS_KILL,
                trigger_hit=True,
                fault_injected=True,
                turn_return_code=137,
            )
        )

    def test_parses_and_pins_astra_session(self):
        session_id = str(uuid.uuid4())
        value = parse_astra_json(
            json.dumps({"session_id": session_id, "success": True}), session_id
        )
        self.assertTrue(value["success"])

    def test_ledger_is_ordered_jsonl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "controller.jsonl"
            ledger = JsonlLedger(path, "run-1")
            ledger.append("one")
            ledger.append("two", value=2)
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual([row["sequence"] for row in rows], [1, 2])
            self.assertEqual(rows[1]["value"], 2)
            self.assertTrue(all(isinstance(row["monotonic_ns"], int) for row in rows))

    def test_astra_args_can_disable_explicit_memory_tool(self):
        cold_args = astra_args(
            remote_binary="astra",
            model_name="model-id",
            max_turns=50,
            session_id=None,
            permission_mode="auto",
            read_memory=False,
        )
        warm_args = astra_args(
            remote_binary="astra",
            model_name="model-id",
            max_turns=50,
            session_id=None,
            permission_mode="auto",
            read_memory=True,
        )
        self.assertIn("--disallowed-tools", cold_args)
        self.assertEqual(
            cold_args[cold_args.index("--disallowed-tools") + 1],
            "memory",
        )
        self.assertNotIn("--disallowed-tools", warm_args)

    def test_astra_args_can_omit_max_turns_for_new_cli(self):
        args = astra_args(
            remote_binary="astra",
            model_name="glm-5.2(thinking:high)",
            max_turns=None,
            session_id=None,
            permission_mode="auto",
            read_memory=False,
        )
        self.assertNotIn("--max-turns", args)
        self.assertIn("chat", args)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, condition):
        events = []
        environment = FakeEnvironment(condition)
        controller = FaultController(
            ControllerConfig(
                condition=condition,
                trigger_path="/tmp/astra-smoke/trigger",
                identity_path="/tmp/astra-smoke/identity.json",
                probe_path="/installed-agent/probe.py",
                expected_exe="/installed-agent/astra",
            ),
            lambda event, **fields: events.append((event, fields)),
        )
        outcome = await controller.run(environment, asyncio.Event())
        return outcome, events, environment.commands

    async def test_c0_observes_same_trigger_but_does_not_kill(self):
        outcome, events, commands = await self._run(CLEAN)
        self.assertTrue(outcome.trigger_hit)
        self.assertFalse(outcome.fault_injected)
        self.assertFalse(any(" kill " in command for command in commands))
        self.assertIn(("fault_action", {"action": "noop", "executed": True}), events)

    async def test_f1_kills_registered_tree_through_probe_and_checks_environment(self):
        outcome, events, commands = await self._run(PROCESS_KILL)
        self.assertTrue(outcome.fault_injected)
        self.assertTrue(any(" kill " in command for command in commands))
        self.assertEqual(commands[-1], "true")
        self.assertTrue(
            any(
                event == "fault_action"
                and fields["action"] == "freeze_kill_tree_sigkill"
                and fields["targeted_descendant_pids"] == [1235]
                for event, fields in events
            )
        )
        self.assertTrue(
            any(event == "task_environment_post_fault_probe" for event, _ in events)
        )
