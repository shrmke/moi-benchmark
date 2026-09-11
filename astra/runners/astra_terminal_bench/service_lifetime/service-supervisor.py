#!/usr/bin/env python3
"""Retain ownership of a declared service across fork/setsid/daemonization."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

libc = ctypes.CDLL(None, use_errno=True)
if libc.prctl(36, 1, 0, 0, 0) != 0:
    raise OSError(ctypes.get_errno(), 'Cannot become service subreaper')
root = os.getpid()
events = Path('/opt/moi-service-lifetime/registry/%s-supervisor.jsonl' % os.getppid())
def emit(event, **fields):
    with events.open('a') as stream:
        stream.write(json.dumps(dict(event=event, time=time.time(), supervisor_pid=root, **fields)) + '\n')

def table():
    result = {}
    for p in Path('/proc').iterdir():
        if not p.name.isdigit():
            continue
        try:
            fields = (p/'stat').read_text().rsplit(')', 1)[1].split()
            if fields[0] != 'Z':
                result[int(p.name)] = (int(fields[1]), int(fields[19]))
        except (OSError, ValueError, IndexError):
            pass
    return result

def descendants(t):
    selected = {root}
    while True:
        updated = selected | {pid for pid, (parent, _) in t.items() if parent in selected}
        if updated == selected:
            return selected - {root}
        selected = updated

def stop_children():
    # Never signal by an unverified numeric PID. Include adopted daemon children.
    handles = {}
    try:
        start = time.monotonic()
        while time.monotonic() - start < 3:
            t = table()
            live = descendants(t)
            if not live:
                return
            for pid in live:
                try:
                    identity = (pid, t[pid][1])
                    fd = handles.get(identity)
                    if fd is None:
                        fd = os.pidfd_open(pid)
                        now = table().get(pid)
                        if now is None or now[1] != identity[1]:
                            os.close(fd)
                            continue
                        handles[identity] = fd
                    signal.pidfd_send_signal(fd, signal.SIGTERM if time.monotonic()-start < 1 else signal.SIGKILL)
                except ProcessLookupError:
                    pass
            time.sleep(.02)
        if descendants(table()):
            raise RuntimeError('Service descendants survived shutdown')
    finally:
        for fd in handles.values():
            os.close(fd)

requested = []
def on_signal(signum, frame):
    requested.append(signum)
for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
    signal.signal(sig, on_signal)
child = subprocess.Popen(['bash', '-c', sys.argv[1]], stdin=subprocess.DEVNULL)
emit('launcher_started', launcher_pid=child.pid)
launcher_code = 0
try:
    while not requested:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            emit('all_service_children_exited', launcher_return_code=launcher_code)
            raise SystemExit(launcher_code)
        if pid:
            code = os.waitstatus_to_exitcode(status)
            emit('child_reaped', child_pid=pid, return_code=code, launcher=(pid == child.pid))
            if pid == child.pid:
                launcher_code = code if code >= 0 else 128-code
                child.returncode = code
                emit('launcher_exited_service_supervision_continues')
        else:
            time.sleep(.05)
    emit('shutdown_requested', signal=requested[0])
finally:
    stop_children()
    emit('supervisor_finished')
