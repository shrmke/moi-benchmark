"""Offline diagnostics and supported session deletion for the shared test catalog."""
from __future__ import annotations
import datetime
import gzip
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import pymysql

ROOT = Path(__file__).resolve().parents[3]
DATABASE = "astra_toolathlon_shared_969550b"
CORE = ("agent_sessions", "agent_runs", "inference_invocations", "inference_invocation_settlement_debts")

def connect():
    config = Path(os.environ.get("TOOLATHLON_ASTRA_SERVER_CONFIG", str(ROOT / "work/toolathlon-astra-969550b/server-runtime.private.json")))
    env = json.loads(config.read_text())
    return pymysql.connect(host="127.0.0.1", port=6001, user=env["MATRIXONE_USER"], password=env["MATRIXONE_PASSWORD"], database=DATABASE, connect_timeout=10, read_timeout=60, write_timeout=60, autocommit=True), env

def counts(connection):
    with connection.cursor() as cur:
        cur.execute("SHOW TABLES")
        tables = {row[0] for row in cur.fetchall()}
        result = {}
        for table in CORE:
            if table in tables:
                cur.execute(f"SELECT COUNT(*) FROM `{table}`")
                result[table] = int(cur.fetchone()[0])
        if "infra_llm_models" in tables:
            cur.execute("SELECT COUNT(*) FROM infra_llm_models WHERE is_active <> 0")
            result["active_models"] = int(cur.fetchone()[0])
        return result

def assert_clean(output):
    connection, _ = connect()
    try:
        result = counts(connection)
    finally:
        connection.close()
    if any(result.values()):
        record = {"status": "failed", "phase": "before_start", "remaining": result}
        (Path(output) / "shared-db-cleanup.json").write_text(json.dumps(record, indent=2))
        raise RuntimeError("Shared Astra catalog has residual task state; finish maintenance before resuming")

def docker(*args, timeout=30):
    result = subprocess.run(["sudo", "-n", "docker", *map(str, args)], capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError("Astra maintenance Docker operation failed: " + str(args[0]))
    return result.stdout

def request(base, method, path, token=None, body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as response:
            data = response.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Astra maintenance API {method} {path}: HTTP {exc.code}") from None

def cleanup(output, image="astra-969550b:local"):
    output = Path(output)
    backup = output / ("shared-db-archive-" + datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + secrets.token_hex(3))
    backup.mkdir(mode=0o700, parents=True)
    connection, env = connect()
    name = None
    started = False
    try:
        before = counts(connection)
        with connection.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = [r[0] for r in cur.fetchall()]
            for table in tables:
                if not (table.startswith(("agent_", "inference_", "model_", "work_", "plan", "tool_invocation")) or table == "infra_llm_models"):
                    continue
                cur.execute(f"SELECT * FROM `{table}`")
                columns = [col[0] for col in cur.description]
                rows = cur.fetchall()
                if rows:
                    with gzip.open(backup / (table + ".jsonl.gz"), "wt") as stream:
                        for row in rows:
                            stream.write(json.dumps(dict(zip(columns, row)), default=str) + "\n")
                    os.chmod(backup / (table + ".jsonl.gz"), 0o600)
            # No task server is running here. Disable stale endpoints before recovery workers start.
            if "infra_llm_models" in tables:
                cur.execute("UPDATE infra_llm_models SET is_active=0 WHERE is_active<>0")
            sessions = []
            if "agent_sessions" in tables:
                cur.execute("SELECT session_id,user_id FROM agent_sessions")
                sessions = list(cur.fetchall())
        identities = {}
        if sessions:
            wanted = {user for _, user in sessions}
            for path in (ROOT / "work/toolathlon-astra-969550b").glob("**/product-identity.private.json"):
                value = json.loads(path.read_text())
                if value.get("server_user_id") in wanted:
                    identities[value["server_user_id"]] = value
            if wanted - identities.keys():
                raise RuntimeError("Missing original task credentials for residual Astra sessions")
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            base = f"http://127.0.0.1:{port}"
            name = "toolathlon-astra-maint-" + secrets.token_hex(6)
            env = {k:v for k,v in env.items() if k.startswith(("ASTRA_", "MATRIXONE_")) or k in {"MEMORIA_BASE_URL", "MEMORIA_MASTER_KEY", "RUST_LOG"}}
            env.update(ASTRA_DATABASE=DATABASE, ASTRA_DATABASE_PREFIX="", ASTRA_AUTO_CREATE_DATABASE="true", MATRIXONE_HOST="127.0.0.1", MATRIXONE_PORT="6001", ASTRA_API_HOST="127.0.0.1", ASTRA_API_PORT=str(port), MEMORIA_BASE_URL="http://127.0.0.1:18100", HOME="/tmp", NO_PROXY="*", no_proxy="*")
            with tempfile.TemporaryDirectory(prefix="astra-maint-") as private:
                envfile = Path(private) / "server.env"
                envfile.touch(mode=0o600)
                envfile.write_text("".join(f"{k}={v}\n" for k,v in env.items()))
                docker("run", "-d", "--name", name, "--network", "host", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{os.getuid()}:{os.getgid()}", "--tmpfs", "/tmp:rw,nosuid,nodev,mode=1777", "--env-file", envfile, image)
                started = True
            deadline = time.monotonic() + 180
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Astra maintenance startup timed out")
                    time.sleep(2)
            for session, user in sessions:
                identity = identities[user]
                auth = request(base, "POST", "/auth/login", body={"username": identity["username"], "password": identity["password"]})
                token = auth["access_token"]
                with connection.cursor() as cur:
                    cur.execute("SELECT run_id,status FROM agent_runs WHERE session_id=%s AND user_id=%s", (session,user))
                    runs = cur.fetchall()
                for run, status in runs:
                    if status not in {"completed", "failed", "cancelled"}:
                        request(base, "POST", f"/chat/runs/{run}/cancel", token, {})
                deadline = time.monotonic() + 60
                while True:
                    with connection.cursor() as cur:
                        cur.execute("SELECT COUNT(*) FROM agent_runs WHERE session_id=%s AND user_id=%s AND status NOT IN ('completed','failed','cancelled')", (session,user))
                        active = int(cur.fetchone()[0])
                    if not active:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Astra cancellation did not converge; session retained")
                    time.sleep(2)
                request(base, "DELETE", f"/sessions/{session}", token)
        if started:
            docker("stop", "-t", "10", name)
            (backup / "maintenance-server.log").write_bytes(docker("logs", name))
            docker("rm", name)
            started = False
        after = counts(connection)
        result = {"status": "passed" if not any(after.values()) else "failed", "before": before, "remaining": after, "archive": str(backup)}
        (output / "shared-db-cleanup.json").write_text(json.dumps(result, indent=2))
        if result["status"] != "passed":
            raise RuntimeError("Supported session deletion left residual Astra state")
        return result
    except Exception as exc:
        (output / "shared-db-cleanup.json").write_text(json.dumps({"status":"failed", "error":str(exc), "archive":str(backup)}, indent=2))
        raise
    finally:
        connection.close()
        if started:
            try:
                docker("stop", "-t", "10", name)
                (backup / "maintenance-server.log").write_bytes(docker("logs", name))
                docker("rm", name)
            except Exception:
                pass
