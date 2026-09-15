"""Launch a v2 service with durable output, relayed to the native process tool."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path("/opt/moi-service-lifetime")
key, command = sys.argv[1:3]
marker = ROOT / "registry" / (key + ".json")
log = ROOT / "registry" / (key + ".log")
# The child records its own identity before exec, avoiding parent/child registration races.
register = """
import json,os
from pathlib import Path
root=Path('/opt/moi-service-lifetime')
pid=os.getpid()
fields=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()
record={'pid':pid,'start_ticks':int(fields[19]),'pgid':pid,'sid':pid,
        'requested_ttl_seconds':43200,'effective_max_seconds':43200,
        'lifetime':'container-through-verifier','source':'hermes-background'}
marker=Path(__import__('sys').argv[1])
temporary=marker.with_suffix('.tmp')
temporary.write_text(json.dumps(record)+'\\n')
os.chmod(temporary,0o600)
os.replace(temporary,marker)
os.execv('/usr/bin/timeout',['/usr/bin/timeout','--signal=TERM','--kill-after=5s',
         '43200s','python3',str(root/'service-supervisor.py'),__import__('sys').argv[2]])
"""
with log.open("ab", buffering=0) as output:
    child = subprocess.Popen(
        ["python3", "-c", register, str(marker), command],
        stdin=subprocess.DEVNULL, stdout=output, stderr=output, start_new_session=True)
# The relay belongs to the Agent tree. Its termination must not kill the registered service.
with log.open("rb", buffering=0) as reader:
    try:
        while True:
            data = reader.read(65536)
            if data:
                os.write(1, data)
            elif child.poll() is not None:
                break
            else:
                time.sleep(.05)
    except BrokenPipeError:
        # Agent output is gone; the child continues writing its durable log.
        raise SystemExit(0)
raise SystemExit(child.returncode)
