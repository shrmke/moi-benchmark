"""Supervise a shell call; return its launcher status without waiting for daemons."""
import json
import os
from pathlib import Path
import runpy
import shlex
import subprocess
import sys
import time

ROOT = Path("/opt/moi-service-lifetime")
helper = runpy.run_path(str(ROOT / "hermes-service-lifetime.py"))
key, command = sys.argv[1:3]
marker = ROOT / "registry" / (key + ".json")
log = ROOT / "registry" / (key + ".log")
input_path = ROOT / "registry" / (key + ".stdin")
input_path.write_bytes(sys.stdin.buffer.read())
os.chmod(input_path, 0o600)
command = "exec bash -c %s < %s" % (shlex.quote(command), shlex.quote(str(input_path)))
env = os.environ.copy()
pythonpath = env.pop("PYTHONPATH", None)
if pythonpath is not None:
    command = "export PYTHONPATH=%s; %s" % (shlex.quote(pythonpath), command)
register = """
import json,os,sys
from pathlib import Path
pid=os.getpid()
fields=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()
record={'pid':pid,'start_ticks':int(fields[19]),'pgid':pid,'sid':pid,
        'requested_ttl_seconds':43200,'effective_max_seconds':43200,
        'lifetime':'container-through-verifier','source':'hermes-local'}
marker=Path(sys.argv[1])
temporary=marker.with_suffix('.tmp')
temporary.write_text(json.dumps(record)+'\\n')
os.chmod(temporary,0o600)
os.replace(temporary,marker)
os.execv('/usr/bin/timeout',['/usr/bin/timeout','--signal=TERM','--kill-after=5s',
         '43200s','python3','/opt/moi-service-lifetime/service-supervisor.py',sys.argv[2]])
"""
with log.open("ab", buffering=0) as output:
    child = subprocess.Popen(["python3", "-I", "-c", register, str(marker), command],
        stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True, env=env)
events = ROOT / "registry" / ("%s-supervisor.jsonl" % child.pid)
code = None
try:
    with log.open("rb", buffering=0) as reader:
        while code is None:
            data = reader.read(65536)
            if data:
                os.write(1, data)
            if events.exists():
                for line in events.read_text().splitlines():
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if event.get("event") == "child_reaped" and event.get("launcher"):
                        code = event["return_code"]
            if code is None and child.poll() is not None:
                code = child.returncode
            if code is None:
                time.sleep(.05)
        # Drain output produced before the launcher's completion.
        remaining = max(0, log.stat().st_size - reader.tell())
        while remaining:
            data = reader.read(min(65536, remaining))
            if not data:
                break
            os.write(1, data)
            remaining -= len(data)
    record = helper["live_record"](marker)
    if record and not helper["prune_work"](record):
        helper["stop"](record)
except BaseException:
    record = helper["live_record"](marker)
    if record:
        helper["stop"](record)
    raise
finally:
    input_path.unlink(missing_ok=True)
raise SystemExit(code if code >= 0 else 128-code)
