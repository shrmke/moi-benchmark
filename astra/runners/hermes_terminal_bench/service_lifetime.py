"""Hermes background entry point for the shared v2 service supervisor."""
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import shlex
import signal
import time

ROOT = Path("/opt/moi-service-lifetime")
LAUNCHER = ROOT / "hermes-service-launch.py"


def descendants(root):
    table = {}
    for path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = path.read_text().rsplit(")", 1)[1].split()
            if fields[0] != "Z":
                table[int(path.parent.name)] = (int(fields[1]), int(fields[19]))
        except (OSError, ValueError, IndexError):
            pass
    owned = {root} if root in table else set()
    while True:
        updated = owned | {pid for pid, row in table.items() if row[0] in owned}
        if updated == owned:
            return table, owned
        owned = updated


def live_record(path):
    try:
        record = json.loads(Path(path).read_text())
        fields = Path("/proc/%s/stat" % record["pid"]).read_text().rsplit(")", 1)[1].split()
        if fields[0] != "Z" and int(fields[19]) == record["start_ticks"]:
            return record
    except (OSError, ValueError, KeyError, IndexError):
        pass
    return None


def stop(record):
    try:
        fd = os.pidfd_open(record["pid"])
    except ProcessLookupError:
        return
    try:
        fields = Path("/proc/%s/stat" % record["pid"]).read_text().rsplit(")", 1)[1].split()
        if int(fields[19]) == record["start_ticks"]:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
    except (FileNotFoundError, ProcessLookupError):
        pass
    finally:
        os.close(fd)


def patch_registry(module):
    cls = module.ProcessRegistry
    original_spawn = cls.spawn_local
    original_kill = cls.kill_process

    def spawn(self, command, cwd=None, task_id="", session_key="", env_vars=None, use_pty=False):
        # Interactive PTYs retain native semantics; they are not service declarations.
        if use_pty:
            return original_spawn(self, command, cwd, task_id, session_key, env_vars, use_pty)
        import uuid
        key = uuid.uuid4().hex
        marker = ROOT / "registry" / (key + ".json")
        wrapped = "exec /usr/bin/env -u PYTHONPATH python3 %s %s %s" % (
            shlex.quote(str(LAUNCHER)), shlex.quote(key), shlex.quote(command))
        session = original_spawn(self, wrapped, cwd, task_id, session_key, env_vars, False)
        session.command = command
        session._moi_service_marker = str(marker)
        return session

    def kill(self, session_id, *, source="process.kill", consume_output=True):
        session = self.get(session_id)
        marker = getattr(session, "_moi_service_marker", None)
        record = live_record(marker) if marker else None
        if record and source == "kill_all" and prune_work(record):
            return {"status": "retained_for_verifier", "session_id": session_id}
        if record:
            stop(record)
        return original_kill(self, session_id, source=source, consume_output=consume_output)

    cls.spawn_local = spawn
    cls.kill_process = kill



def service_members(record):
    """Identify actual service processes, then retain their complete child trees."""
    table, owned = descendants(record["pid"])
    if table.get(record["pid"], (None, None))[1] != record["start_ticks"]:
        return table, set(), set()
    found = set()
    listeners = set()
    if record.get("source") == "hermes-background":
        for name in ("tcp", "tcp6"):
            try:
                for line in Path("/proc/net/" + name).read_text().splitlines()[1:]:
                    fields = line.split()
                    if fields[3] == "0A":
                        listeners.add("socket:[" + fields[9] + "]")
            except OSError:
                pass
    for pid in owned:
        try:
            argv = [os.fsdecode(arg) for arg in Path("/proc/%s/cmdline" % pid).read_bytes().split(b"\0") if arg]
            try:
                exe = Path(os.readlink("/proc/%s/exe" % pid).removesuffix(" (deleted)"))
            except PermissionError:
                # Postfix disables proc executable access without SYS_PTRACE.
                # Limit fallback to its exact master entry point and owned PID.
                if not argv or argv[0] not in {"/usr/lib/postfix/sbin/master", "/usr/libexec/postfix/master"}:
                    continue
                fields = Path("/proc/%s/stat" % pid).read_text().rsplit(")", 1)[1].split()
                if fields[0] == "Z" or int(fields[19]) != table[pid][1]:
                    continue
                exe = Path(argv[0])
            named = exe.name in {"nginx", "sshd"} or exe.name.startswith("qemu-system-")
            named = named or (exe.name == "master" and str(exe.parent) in {"/usr/lib/postfix/sbin", "/usr/libexec/postfix"})
            if exe.name.startswith("python"):
                scripts = {"/usr/lib/mailman3/bin/master", "/usr/lib/mailman3/bin/runner", "/usr/bin/mailman", "/usr/bin/mailman3"}
                named = named or any(arg in scripts for arg in argv[1:2])
                named = named or (len(argv) > 2 and argv[1] == "-m" and argv[2] in {"mailman.bin.master", "mailman.bin.runner"})
                if len(argv) > 2 and argv[1] == "-c":
                    import ast
                    try:
                        tree = ast.parse(argv[2])
                        named = named or any(isinstance(node, ast.ImportFrom) and node.module == "mailman.bin.master" for node in ast.walk(tree))
                    except SyntaxError:
                        pass
            if named:
                found.add(pid)
            elif listeners:
                for fd in Path("/proc/%s/fd" % pid).iterdir():
                    try:
                        if os.readlink(fd) in listeners:
                            found.add(pid)
                            break
                    except OSError:
                        pass
        except OSError:
            pass
    keep = set(found)
    while True:
        updated = keep | {pid for pid in owned if table[pid][0] in keep}
        if updated == keep:
            break
        keep = updated
    # Keep the ancestors required to own those service trees, not their siblings.
    while True:
        updated = keep | {table[pid][0] for pid in keep if pid in table and table[pid][0] in owned}
        if updated == keep:
            return table, owned, keep
        keep = updated


def is_service(record):
    return bool(service_members(record)[2])


def prune_work(record):
    """Remove unrelated work before retaining a service supervisor."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        table, owned, keep = service_members(record)
        if not keep:
            return False
        for pid in owned - keep:
            try:
                fd = os.pidfd_open(pid)
                try:
                    fields = Path("/proc/%s/stat" % pid).read_text().rsplit(")", 1)[1].split()
                    if int(fields[19]) == table[pid][1]:
                        signal.pidfd_send_signal(fd, sig)
                finally:
                    os.close(fd)
            except (FileNotFoundError, ProcessLookupError):
                pass
        if owned - keep:
            time.sleep(.1)
    return True


def patch_local(module):
    cls = module.LocalEnvironment
    original_run = cls._run_bash
    original_kill = cls._kill_process

    def run(self, cmd_string, *, login=False, timeout=120, stdin_data=None):
        if login:
            return original_run(self, cmd_string, login=login, timeout=timeout, stdin_data=stdin_data)
        import uuid
        key = uuid.uuid4().hex
        command = "exec python3 -I %s %s %s" % (
            shlex.quote(str(ROOT / "hermes-local-launch.py")), key, shlex.quote(cmd_string))
        proc = original_run(self, command, login=False, timeout=timeout, stdin_data=stdin_data)
        proc._moi_service_marker = str(ROOT / "registry" / (key + ".json"))
        return proc

    def kill(self, proc):
        marker = getattr(proc, "_moi_service_marker", None)
        record = live_record(marker) if marker else None
        if record:
            stop(record)
        return original_kill(self, proc)

    cls._run_bash = run
    cls._kill_process = kill


class RegistryFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in {"tools.process_registry", "tools.environments.local"}:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        original = spec.loader
        class Loader(importlib.abc.Loader):
            def create_module(self, spec):
                return original.create_module(spec)
            def exec_module(self, module):
                original.exec_module(module)
                (patch_registry if fullname == "tools.process_registry" else patch_local)(module)
        spec.loader = Loader()
        return spec


def install_hook():
    import sys
    for name, patch in (("tools.process_registry", patch_registry), ("tools.environments.local", patch_local)):
        if name in sys.modules:
            patch(sys.modules[name])
    sys.meta_path.insert(0, RegistryFinder())


async def install(agent, environment):
    from astra.runners.astra_terminal_bench.service_lifetime import install as shared_install
    await shared_install(agent, environment)
    directory = Path(__file__).parent
    for local, remote in (
        (directory / "service_lifetime.py", ROOT / "hermes-service-lifetime.py"),
        (directory / "service_launch.py", LAUNCHER),
        (directory / "service_local_launch.py", ROOT / "hermes-local-launch.py"),
        (directory / "service_probe.py", Path("/installed-agent/hermes-service-probe.py")),
    ):
        await environment.upload_file(local, str(remote))
