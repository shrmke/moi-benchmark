#!/usr/bin/env python3
"""Prepare and record local competitor service deployments.

This script keeps vendor source, compose files, volumes, logs, and manifests
under ``.local-services``.  Only this small launcher and sanitized templates
belong in Git; vendor repositories are never copied into the benchmark tree.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ROOT = REPO_ROOT / ".local-services"
VERSION_FILE = REPO_ROOT / "local-rag-platforms" / "versions.json"


def run(command: list[str], *, cwd: Path | None = None) -> tuple[int, str]:
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    output = (completed.stdout or completed.stderr).strip()
    return completed.returncode, output


def load_manifest() -> dict[str, Any]:
    return json.loads(VERSION_FILE.read_text(encoding="utf-8"))


def service_config(system_id: str) -> dict[str, Any]:
    services = load_manifest()["services"]
    try:
        return services[system_id]
    except KeyError as exc:
        raise SystemExit(f"unknown system: {system_id}") from exc


def service_dir(system_id: str) -> Path:
    target = RUNTIME_ROOT / system_id
    for name in ("source", "compose", "data", "logs"):
        (target / name).mkdir(parents=True, exist_ok=True)
    return target


def _credentials_path(system_id: str) -> Path:
    return service_dir(system_id) / "credentials.env"


def _read_credentials(system_id: str) -> dict[str, str]:
    path = _credentials_path(system_id)
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


def _write_credentials(system_id: str, values: dict[str, str]) -> Path:
    path = _credentials_path(system_id)
    payload = "".join(f"{key}={values[key]}\n" for key in sorted(values))
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
    path.chmod(0o600)
    return path


def ensure_credentials(system_id: str) -> dict[str, Any]:
    """Create non-committed bootstrap credentials without printing values."""

    prefix = system_id.removesuffix("_local").upper()
    values = _read_credentials(system_id)
    defaults = {
        f"{prefix}_LOCAL_ADMIN_EMAIL": f"{prefix.lower()}-local@localhost.invalid",
        f"{prefix}_LOCAL_ADMIN_NAME": "MOI_Benchmark",
        f"{prefix}_LOCAL_ADMIN_PASSWORD": secrets.token_urlsafe(24),
    }
    created: list[str] = []
    for key, value in defaults.items():
        if key not in values:
            values[key] = value
            created.append(key)
    path = _write_credentials(system_id, values)
    result = {
        "system_id": system_id,
        "path": str(path.relative_to(REPO_ROOT)),
        "mode": oct(path.stat().st_mode & 0o777),
        "created_names": created,
        "stored_names": sorted(values),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def set_secret(system_id: str, name: str) -> dict[str, Any]:
    """Store one secret read from stdin without echoing it."""

    if not name or not name.replace("_", "A").isalnum() or not name[0].isalpha():
        raise SystemExit("secret name must be an environment variable identifier")
    value = sys.stdin.read().strip()
    if not value:
        raise SystemExit("refusing to store an empty secret")
    values = _read_credentials(system_id)
    values[name] = value
    path = _write_credentials(system_id, values)
    result = {
        "system_id": system_id,
        "path": str(path.relative_to(REPO_ROOT)),
        "mode": oct(path.stat().st_mode & 0o777),
        "stored_name": name,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def preflight() -> dict[str, Any]:
    commands = {
        "docker_version": ["docker", "version", "--format", "{{.Client.Version}}/{{.Server.Version}}"],
        "compose_version": ["docker", "compose", "version", "--short"],
        "docker_platform": ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"],
        "docker_context": ["docker", "context", "show"],
    }
    checks: dict[str, Any] = {}
    for name, command in commands.items():
        code, output = run(command)
        checks[name] = {"command": command, "exit_code": code, "output": output}
    memory = None
    code, output = run(["sysctl", "-n", "hw.memsize"])
    if code == 0:
        try:
            memory = int(output)
        except ValueError:
            memory = output
    result = {
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": {
            "os": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "memory_bytes": memory,
        },
        "checks": checks,
    }
    target = RUNTIME_ROOT / "environment-manifest.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _safe_endpoint(value: str) -> str:
    """Return an endpoint suitable for artifacts without userinfo or queries."""

    parsed = urlparse(value)
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        return "<invalid-endpoint>"
    try:
        port = f":{parsed.port}" if parsed.port is not None else ""
    except ValueError:
        return "<invalid-endpoint>"
    path = parsed.path or "/"
    return f"{parsed.scheme}://{parsed.hostname}{port}{path}"


def _http_readiness(url: str, timeout: float) -> dict[str, Any]:
    check: dict[str, Any] = {
        "name": "http",
        "type": "http",
        "endpoint": _safe_endpoint(url),
        "status": "BLOCKED",
        "ready": False,
    }
    if check["endpoint"] == "<invalid-endpoint>":
        check["error"] = "INVALID_ENDPOINT"
        return check
    try:
        request = Request(url, headers={"Accept": "application/json, text/plain, */*", "User-Agent": "MOI-Deployment-Readiness/1"})
        with urlopen(request, timeout=timeout) as response:
            status_code = int(response.getcode())
        check["http_status"] = status_code
        check["ready"] = 200 <= status_code < 400
        check["status"] = "READY" if check["ready"] else "BLOCKED"
        if not check["ready"]:
            check["error"] = f"HTTP_{status_code}"
    except HTTPError as exc:
        check["http_status"] = int(exc.code)
        check["error"] = f"HTTP_{exc.code}"
    except (URLError, OSError, TimeoutError, ValueError) as exc:
        check["error"] = type(exc).__name__
    return check


def _tcp_readiness(host: str, port: int, timeout: float) -> dict[str, Any]:
    endpoint = f"tcp://{host}:{port}"
    check: dict[str, Any] = {
        "name": "tcp",
        "type": "tcp",
        "endpoint": endpoint,
        "status": "BLOCKED",
        "ready": False,
    }
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            pass
    except (OSError, TimeoutError, ValueError) as exc:
        check["error"] = type(exc).__name__
        return check
    check["status"] = "READY"
    check["ready"] = True
    return check


def readiness(
    system_id: str,
    *,
    runtime_root: Path = RUNTIME_ROOT,
    url: str | None = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    """Run a read-only service probe and persist a machine-readable result."""

    config = service_config(system_id)
    health = config.get("health") or {}
    health_type = str(health.get("type", "")).lower()
    if url is not None or health_type == "http":
        check = _http_readiness(url or str(health.get("url", "")), timeout)
    elif health_type == "tcp":
        check = _tcp_readiness(str(health.get("host", "127.0.0.1")), int(health.get("port", 0)), timeout)
    else:
        check = {
            "name": "health-contract",
            "type": "unknown",
            "endpoint": "<not-configured>",
            "status": "BLOCKED",
            "ready": False,
            "error": "HEALTH_CONTRACT_MISSING",
        }

    result: dict[str, Any] = {
        "schema": "moi-deployment-readiness-v1",
        "system_id": system_id,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "version": config.get("version") or config.get("tag"),
        "image": config.get("image"),
        "ready": bool(check.get("ready")),
        "status": "READY" if check.get("ready") else "BLOCKED",
        "checks": [check],
    }
    output = Path(runtime_root) / system_id / "logs" / "readiness.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def prepare(system_id: str) -> dict[str, Any]:
    config = service_config(system_id)
    target = service_dir(system_id)
    repo_url = config["repo"]
    tag = config["tag"]
    repo_name = repo_url.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    source = target / "source" / repo_name
    if not source.exists():
        code, output = run(
            ["git", "clone", "--filter=blob:none", "--depth", "1", "--branch", tag, repo_url, str(source)],
            cwd=REPO_ROOT,
        )
        if code != 0:
            raise SystemExit(f"failed to clone {system_id}: {output}")
    else:
        code, output = run(["git", "-C", str(source), "rev-parse", "HEAD"])
        if code != 0:
            raise SystemExit(f"source exists but is not a git checkout: {source}")

    checkout_code, checkout = run(["git", "-C", str(source), "rev-parse", "HEAD"])
    tag_code, resolved_tag = run(["git", "-C", str(source), "describe", "--tags", "--exact-match", "HEAD"])
    record = {
        "system_id": system_id,
        "repo": repo_url,
        "requested_tag": tag,
        "source": str(source.relative_to(REPO_ROOT)),
        "commit": checkout if checkout_code == 0 else None,
        "resolved_tag": resolved_tag if tag_code == 0 else None,
        "compose_locator": config.get("compose_locator"),
        "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "runtime_root": str(target.relative_to(REPO_ROOT)),
    }
    (target / "compose" / "source-lock.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(record, ensure_ascii=False, indent=2))
    return record


def _compose_file(system_id: str, source: Path) -> Path | None:
    configured = service_config(system_id).get("compose_locator")
    if configured and configured.endswith((".yml", ".yaml")):
        candidate = source / configured
        if candidate.is_file():
            return candidate
    candidates = sorted(
        path
        for path in source.rglob("*")
        if path.is_file() and path.name in {"docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"}
    )
    return candidates[0] if candidates else None


def record(system_id: str) -> dict[str, Any]:
    config = service_config(system_id)
    target = service_dir(system_id)
    repo_name = config["repo"].rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
    source = target / "source" / repo_name
    compose_file = _compose_file(system_id, source) if source.is_dir() else None
    result: dict[str, Any] = {
        "system_id": system_id,
        "deployment_mode": "self_hosted",
        "model_egress": "external",
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requested": config,
        "source": {},
        "compose": {},
        "images": [],
    }
    if source.is_dir():
        code, commit = run(["git", "-C", str(source), "rev-parse", "HEAD"])
        result["source"] = {"path": str(source.relative_to(REPO_ROOT)), "commit": commit if code == 0 else None}
    if compose_file:
        result["compose"]["file"] = str(compose_file.relative_to(REPO_ROOT))
        project = f"moi_{system_id}"
        code, images = run(["docker", "compose", "-p", project, "-f", str(compose_file), "config", "--images"])
        result["compose"].update({"project": project, "exit_code": code, "images": images.splitlines() if images else []})
        for image in result["compose"]["images"]:
            inspect_code, inspect = run(["docker", "image", "inspect", image, "--format", "{{json .}}"])
            item: dict[str, Any] = {"image": image, "exit_code": inspect_code}
            if inspect_code == 0:
                try:
                    parsed = json.loads(inspect)
                    item.update({"architecture": parsed.get("Architecture"), "os": parsed.get("Os"), "repo_digests": parsed.get("RepoDigests", [])})
                except json.JSONDecodeError:
                    item["raw"] = inspect
            else:
                item["error"] = inspect
            result["images"].append(item)
    else:
        result["compose"]["note"] = "No compose file resolved; use the platform-specific deployment instructions."
    output = target / "logs" / "deployment-manifest.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def _registry_reference(image: str) -> tuple[str, str, str]:
    parts = image.split("/", 1)
    if len(parts) == 1 or ("." not in parts[0] and ":" not in parts[0] and parts[0] != "localhost"):
        registry = "registry-1.docker.io"
        repository = image if len(parts) == 1 else image
        if "/" not in repository:
            repository = f"library/{repository}"
    else:
        registry, repository = parts
    name, separator, tag = repository.rpartition(":")
    if not separator or "/" in tag:
        name, tag = repository, "latest"
    return registry, name, tag


def _registry_token(registry: str, repository: str) -> str | None:
    if registry == "registry-1.docker.io":
        auth_url = (
            "https://auth.docker.io/token?service=registry.docker.io&scope="
            f"repository:{quote(repository, safe='/:')}%3Apull"
        )
    elif registry == "ghcr.io":
        auth_url = (
            "https://ghcr.io/token?service=ghcr.io&scope="
            f"repository:{quote(repository, safe='/:')}%3Apull"
        )
    else:
        return None
    try:
        with urlopen(Request(auth_url, headers={"User-Agent": "MOI-RAG-Local-Smoke/0.1"}), timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload.get("token") or payload.get("access_token")
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def inspect_image(system_id: str, image: str, record_name: str | None = None) -> dict[str, Any]:
    """Record an image manifest through the registry HTTP API.

    Docker-in-Colima can intermittently fail the daemon-side registry request
    with EOF even when the host can reach the registry.  This independent
    read-only probe keeps the architecture gate auditable without pulling or
    starting a container.
    """

    target = service_dir(system_id)
    registry, repository, tag = _registry_reference(image)
    manifest_url = f"https://{registry}/v2/{repository}/manifests/{tag}"
    headers = {
        "Accept": ", ".join(
            (
                "application/vnd.oci.image.index.v1+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.docker.distribution.manifest.v2+json",
            )
        ),
        "User-Agent": "MOI-RAG-Local-Smoke/0.1",
    }
    token = _registry_token(registry, repository)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    result: dict[str, Any] = {
        "system_id": system_id,
        "image": image,
        "registry": registry,
        "repository": repository,
        "tag": tag,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "manifest_url": manifest_url,
        "status": "error",
        "platforms": [],
    }
    try:
        with urlopen(Request(manifest_url, headers=headers), timeout=30) as response:
            manifest = json.loads(response.read().decode("utf-8"))
            result["media_type"] = response.headers.get("Content-Type")
        manifests = manifest.get("manifests") or []
        if manifests:
            result["platforms"] = [
                {
                    "os": item.get("platform", {}).get("os"),
                    "architecture": item.get("platform", {}).get("architecture"),
                    "variant": item.get("platform", {}).get("variant"),
                    "digest": item.get("digest"),
                }
                for item in manifests
            ]
        else:
            config_digest = (manifest.get("config") or {}).get("digest")
            result["manifest_digest"] = config_digest
            if config_digest:
                blob_url = f"https://{registry}/v2/{repository}/blobs/{config_digest}"
                with urlopen(Request(blob_url, headers=headers), timeout=30) as response:
                    config = json.loads(response.read().decode("utf-8"))
                result["platforms"] = [{
                    "os": config.get("os"),
                    "architecture": config.get("architecture"),
                    "variant": config.get("variant"),
                    "created": config.get("created"),
                }]
        result["status"] = "ready"
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    stem = record_name or image.replace("/", "_").replace(":", "_").replace("@", "_")
    output = target / "logs" / f"image-manifest-{stem}.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


def record_event(system_id: str, event: str, status: str, reason: str | None, details: str) -> dict[str, Any]:
    """Persist a sanitized deployment event supplied by the launcher."""

    target = service_dir(system_id) / "logs" / "deployment-events.jsonl"
    item = {
        "system_id": system_id,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "event": event,
        "status": status,
        "reason": reason,
        "details": details,
    }
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(item, ensure_ascii=False) + "\n")
    print(json.dumps(item, ensure_ascii=False, indent=2))
    return item


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="prepare_local_services")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="record Docker/Colima host facts")
    prepare_parser = sub.add_parser("prepare", help="clone a pinned vendor source into runtime storage")
    prepare_parser.add_argument("system", choices=("dify_local", "fastgpt_local", "ragflow_local", "maxkb_local"))
    record_parser = sub.add_parser("record", help="record source, compose, and image facts")
    record_parser.add_argument("system", choices=("dify_local", "fastgpt_local", "ragflow_local", "maxkb_local"))
    readiness_parser = sub.add_parser("readiness", help="write a machine-readable read-only service readiness result")
    readiness_parser.add_argument("system", choices=("moi_matrixone", "dify_local", "fastgpt_local", "ragflow_local", "maxkb_local"))
    readiness_parser.add_argument("--url", help="override the configured HTTP health endpoint")
    readiness_parser.add_argument("--runtime-root", type=Path, default=RUNTIME_ROOT)
    readiness_parser.add_argument("--timeout", type=float, default=5.0)
    image_parser = sub.add_parser("inspect-image", help="record a registry image manifest without pulling it")
    image_parser.add_argument("system", choices=("dify_local", "fastgpt_local", "ragflow_local", "maxkb_local"))
    image_parser.add_argument("image")
    image_parser.add_argument("--record-name")
    event_parser = sub.add_parser("record-event", help="append a deployment event to the ignored runtime log")
    event_parser.add_argument("system", choices=("dify_local", "fastgpt_local", "ragflow_local", "maxkb_local"))
    event_parser.add_argument("event")
    event_parser.add_argument("status")
    event_parser.add_argument("--reason")
    event_parser.add_argument("--details", required=True)
    credentials_parser = sub.add_parser(
        "ensure-credentials", help="create ignored local bootstrap credentials without printing values"
    )
    credentials_parser.add_argument(
        "system", choices=("dify_local", "fastgpt_local", "ragflow_local", "maxkb_local")
    )
    secret_parser = sub.add_parser("set-secret", help="store a secret read from stdin in the ignored credential file")
    secret_parser.add_argument("system", choices=("dify_local", "fastgpt_local", "ragflow_local", "maxkb_local"))
    secret_parser.add_argument("name")
    args = parser.parse_args(argv)
    if args.command == "preflight":
        preflight()
    elif args.command == "prepare":
        prepare(args.system)
    elif args.command == "record":
        record(args.system)
    elif args.command == "readiness":
        result = readiness(args.system, runtime_root=args.runtime_root, url=args.url, timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1
    elif args.command == "inspect-image":
        inspect_image(args.system, args.image, args.record_name)
    elif args.command == "record-event":
        record_event(args.system, args.event, args.status, args.reason, args.details)
    elif args.command == "ensure-credentials":
        ensure_credentials(args.system)
    elif args.command == "set-secret":
        set_secret(args.system, args.name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
