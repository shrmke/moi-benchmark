from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _load_access_token(credentials_dir: Path) -> str:
    value = json.loads(
        (credentials_dir / "credentials.json").read_text(encoding="utf-8")
    )
    current_profile = value.get("current_profile", "default")
    profile = value.get("profiles", {}).get(current_profile, {})
    token = profile.get("access_token")
    if not isinstance(token, str) or not token:
        raise RuntimeError("Astra credentials contain no access token")
    return token


def _get_json(api_url: str, token: str, path: str, query: dict[str, str] | None = None):
    url = api_url.rstrip("/") + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def _post_json(api_url: str, token: str, path: str, body: dict[str, Any]):
    request = urllib.request.Request(
        api_url.rstrip("/") + path,
        data=json.dumps(body, separators=(",", ":")).encode(),
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.load(response)


def register_session(
    *,
    api_url: str,
    credentials_dir: Path,
    controller_run_id: str,
    task_id: str,
) -> dict[str, Any]:
    uuid.UUID(controller_run_id)
    token = _load_access_token(credentials_dir)
    value = _post_json(
        api_url,
        token,
        "/sessions",
        {
            "title": f"Terminal-Bench C0: {task_id}",
            "metadata": {
                "condition": "C0",
                "controller_run_id": controller_run_id,
                "full_llm_capture": True,
                "task_id": task_id,
            },
        },
    )
    session_id = value.get("session_id") if isinstance(value, dict) else None
    if not isinstance(session_id, str):
        raise RuntimeError("session registration returned no session_id")
    uuid.UUID(session_id)
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def discover_session_id(sessions_root: Path) -> str:
    candidates: set[str] = set()
    if sessions_root.is_dir():
        for path in sessions_root.rglob("*"):
            names = [path.name]
            if path.is_file() and path.suffix == ".jsonl":
                names.append(path.stem)
            for name in names:
                try:
                    candidates.add(str(uuid.UUID(name)))
                except ValueError:
                    continue
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected exactly one isolated Astra session, found {len(candidates)}"
        )
    return candidates.pop()


def _copy_local_session(
    sessions_root: Path,
    session_id: str,
    output_root: Path,
) -> list[dict[str, Any]]:
    candidates: list[Path] = []
    legacy = sessions_root / f"{session_id}.jsonl"
    if legacy.is_file() and not legacy.is_symlink():
        candidates.append(legacy)

    if sessions_root.is_dir():
        for root, dirs, files in os.walk(sessions_root, followlinks=False):
            root_path = Path(root)
            dirs[:] = [
                name
                for name in dirs
                if not (root_path / name).is_symlink()
            ]
            relative_parts = root_path.relative_to(sessions_root).parts
            for name in files:
                source = root_path / name
                if (
                    (session_id in relative_parts or name == f"{session_id}.jsonl")
                    and source.is_file()
                    and not source.is_symlink()
                ):
                    candidates.append(source)

    copied: list[dict[str, Any]] = []
    for source in sorted(set(candidates)):
        relative = source.relative_to(sessions_root)
        destination = output_root / "local-sessions" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination, follow_symlinks=False)
        copied.append(
            {
                "path": str(Path("local-sessions") / relative),
                "bytes": destination.stat().st_size,
                "sha256": _sha256(destination),
            }
        )
    return copied


def _validate_local_journal(path: Path, session_id: str) -> dict[str, Any]:
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("local Astra session journal is invalid") from exc
    if not rows or not all(isinstance(row, dict) for row in rows):
        raise RuntimeError("local Astra session journal is empty or invalid")
    event_types = [row.get("type") for row in rows]
    if not all(isinstance(event_type, str) for event_type in event_types):
        raise RuntimeError("local Astra session journal has an invalid event type")
    if event_types[0] != "session_start":
        raise RuntimeError("local Astra session journal has no opening session_start")
    if event_types.count("session_start") != 1:
        raise RuntimeError("local Astra session journal has multiple session starts")
    if event_types.count("session_end") > 1 or (
        "session_end" in event_types and event_types[-1] != "session_end"
    ):
        raise RuntimeError("local Astra session journal has an invalid session end")
    if not any(
        event_type not in {"session_start", "session_end"}
        for event_type in event_types
    ):
        raise RuntimeError("local Astra session journal has no agent activity")
    for row in rows:
        if row.get("session_id") != session_id:
            raise RuntimeError("local Astra session journal has a mismatched session_id")
    return {
        "event_count": len(rows),
        "terminal_event": rows[-1].get("type"),
    }


def _bundle_file(root: Path, relative_value: Any) -> Path:
    if not isinstance(relative_value, str) or not relative_value:
        raise RuntimeError("trajectory manifest contains an invalid file path")
    relative = Path(relative_value)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError("trajectory manifest file path escapes the bundle")
    path = root / relative
    if not path.is_file() or path.is_symlink():
        raise RuntimeError("trajectory manifest references a missing or unsafe file")
    resolved_root = root.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(resolved_root):
        raise RuntimeError("trajectory manifest file resolves outside the bundle")
    current = path.parent
    while current != root:
        if current.is_symlink():
            raise RuntimeError("trajectory manifest traverses a symlink")
        current = current.parent
    return path


def validate_trajectory_bundle(
    output_root: Path,
    *,
    session_id: str,
    terminal_status: str,
) -> dict[str, Any]:
    """Validate a downloaded Astra trajectory against its manifest."""

    uuid.UUID(session_id)
    output_root = output_root.resolve()
    manifest_path = _bundle_file(output_root, "manifest.json")
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("Astra trajectory manifest is invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise RuntimeError("Astra trajectory manifest schema is invalid")
    if (
        manifest.get("session_id") != session_id
        or manifest.get("product_terminal_status") != terminal_status
        or manifest.get("failed") is not (terminal_status != "completed")
    ):
        raise RuntimeError("Astra trajectory manifest identity is inconsistent")
    if manifest.get("capture_status") != "complete":
        raise RuntimeError("Astra trajectory bundle is not complete")
    if manifest.get("errors") != []:
        raise RuntimeError("complete Astra trajectory manifest contains errors")

    server_session_path = _bundle_file(output_root, "server-session.json")
    server_events_path = _bundle_file(output_root, "server-events.jsonl")
    if (
        manifest.get("server_session_saved") is not True
        or _sha256(server_session_path) != manifest.get("server_session_sha256")
        or manifest.get("server_events_saved") is not True
        or _sha256(server_events_path) != manifest.get("server_events_sha256")
    ):
        raise RuntimeError("Astra server trajectory hashes are inconsistent")
    try:
        server_session = json.loads(
            server_session_path.read_text(encoding="utf-8")
        )
        server_events = [
            json.loads(line)
            for line in server_events_path.read_text(
                encoding="utf-8"
            ).splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Astra server trajectory JSON is invalid") from exc
    event_ids = [
        event.get("event_id")
        for event in server_events
        if isinstance(event, dict)
    ]
    if (
        not isinstance(server_session, dict)
        or server_session.get("session_id") != session_id
        or not server_events
        or not all(
            isinstance(event, dict)
            and event.get("session_id") == session_id
            and isinstance(event.get("event_id"), str)
            and bool(event["event_id"])
            and isinstance(event.get("event_type"), str)
            and bool(event["event_type"])
            and isinstance(event.get("content"), str)
            for event in server_events
        )
        or len(event_ids) != len(set(event_ids))
        or manifest.get("server_event_count") != len(server_events)
    ):
        raise RuntimeError("Astra server trajectory identity or count is invalid")

    local_rows = manifest.get("local_files")
    if not isinstance(local_rows, list) or not local_rows:
        raise RuntimeError("Astra trajectory manifest has no local session files")
    listed_paths: set[str] = set()
    local_trace_count = 0
    tool_result_count = 0
    for row in local_rows:
        if not isinstance(row, dict):
            raise RuntimeError("Astra local trajectory manifest row is invalid")
        relative = row.get("path")
        if (
            not isinstance(relative, str)
            or not relative.startswith("local-sessions/")
            or relative in listed_paths
        ):
            raise RuntimeError("Astra local trajectory manifest path is invalid")
        listed_paths.add(relative)
        path = _bundle_file(output_root, relative)
        if (
            row.get("bytes") != path.stat().st_size
            or row.get("sha256") != _sha256(path)
        ):
            raise RuntimeError("Astra local trajectory file hash is inconsistent")
        if path.stat().st_size > 0 and (
            relative.endswith(f"/{session_id}.jsonl")
            or relative.endswith("/step_events.jsonl")
        ):
            local_trace_count += 1
        if "/tool-results/" in f"/{relative}":
            tool_result_count += 1

    local_root = output_root / "local-sessions"
    actual_local_paths = (
        {
            str(path.relative_to(output_root))
            for path in local_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        if local_root.is_dir()
        else set()
    )
    if actual_local_paths != listed_paths:
        raise RuntimeError("Astra local trajectory manifest is not exhaustive")
    if (
        manifest.get("local_file_count") != len(local_rows)
        or manifest.get("local_trace_file_count") != local_trace_count
        or manifest.get("tool_result_file_count") != tool_result_count
    ):
        raise RuntimeError("Astra local trajectory counts are inconsistent")

    journal_path_value = manifest.get("local_journal_path")
    journal_path = _bundle_file(output_root, journal_path_value)
    journal_summary = _validate_local_journal(journal_path, session_id)
    if (
        manifest.get("local_journal_saved") is not True
        or journal_path_value not in listed_paths
        or manifest.get("local_journal_sha256") != _sha256(journal_path)
        or manifest.get("local_journal_event_count")
        != journal_summary["event_count"]
        or manifest.get("local_journal_terminal_event")
        != journal_summary["terminal_event"]
        or (
            terminal_status == "completed"
            and journal_summary["terminal_event"] != "session_end"
        )
    ):
        raise RuntimeError("Astra local session journal is inconsistent")

    if any(path.is_symlink() for path in output_root.rglob("*")):
        raise RuntimeError("Astra trajectory bundle contains a symlink")
    expected_paths = listed_paths | {
        "manifest.json",
        "server-session.json",
        "server-events.jsonl",
    }
    actual_paths = {
        str(path.relative_to(output_root))
        for path in output_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    if actual_paths != expected_paths:
        raise RuntimeError("Astra trajectory bundle contains unmanifested files")
    return {
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "trajectory_file_count": len(actual_paths),
        "local_file_count": len(local_rows),
        "local_trace_file_count": local_trace_count,
        "tool_result_file_count": tool_result_count,
        "server_event_count": len(server_events),
        "local_journal_event_count": journal_summary["event_count"],
        "local_journal_terminal_event": journal_summary["terminal_event"],
    }


def _load_server_events(
    api_url: str,
    token: str,
    session_id: str,
) -> list[dict[str, Any]]:
    cursor: dict[str, str] = {}
    seen_cursors: set[tuple[str, str]] = set()
    seen_event_ids: set[str] = set()
    collected: list[dict[str, Any]] = []
    expected_total: int | None = None
    while True:
        page = _get_json(
            api_url,
            token,
            f"/events/session/{urllib.parse.quote(session_id, safe='')}",
            {"limit": "100", **cursor},
        )
        events = page.get("events")
        if not isinstance(events, list):
            raise RuntimeError("session events response has no events list")
        page_total = page.get("total")
        if type(page_total) is not int or page_total < 0:
            raise RuntimeError("session events response has an invalid total")
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise RuntimeError("session events total changed during pagination")
        for event in events:
            event_id = event.get("event_id") if isinstance(event, dict) else None
            if not isinstance(event_id, str) or not event_id:
                raise RuntimeError("session event has no event_id")
            if event_id in seen_event_ids:
                raise RuntimeError(
                    "session events response contains a duplicate event"
                )
            seen_event_ids.add(event_id)
            event = _get_json(
                api_url,
                token,
                f"/events/{urllib.parse.quote(event_id, safe='')}",
            )
            if (
                not isinstance(event, dict)
                or event.get("event_id") != event_id
                or event.get("session_id") != session_id
            ):
                raise RuntimeError(
                    "session event does not match the requested session"
                )
            collected.append(event)
        next_cursor = page.get("next_cursor")
        if next_cursor is None:
            break
        if not isinstance(next_cursor, dict):
            raise RuntimeError("session events response has an invalid cursor")
        created_at = next_cursor.get("created_at")
        event_id = next_cursor.get("event_id")
        if not isinstance(created_at, str) or not isinstance(event_id, str):
            raise RuntimeError("session events cursor is incomplete")
        cursor_key = (created_at, event_id)
        if cursor_key in seen_cursors:
            raise RuntimeError("session events cursor did not advance")
        seen_cursors.add(cursor_key)
        cursor = {
            "after_created_at": created_at,
            "after_event_id": event_id,
        }
    if expected_total is None or len(collected) != expected_total:
        raise RuntimeError("session events count does not match the API total")
    return collected


def _export_server_events(
    api_url: str,
    token: str,
    session_id: str,
    output_path: Path,
) -> int:
    previous: bytes | None = None
    stable_events: list[dict[str, Any]] | None = None
    last_error: RuntimeError | None = None
    for attempt in range(5):
        try:
            events = _load_server_events(api_url, token, session_id)
        except RuntimeError as exc:
            last_error = exc
            previous = None
            if attempt < 4:
                time.sleep(0.2)
            continue
        canonical = json.dumps(
            events,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        if canonical == previous:
            stable_events = events
            break
        previous = canonical
        if attempt < 4:
            time.sleep(0.2)
    if stable_events is None:
        raise RuntimeError(
            "session events did not reach a stable complete snapshot"
        ) from last_error

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as stream:
        for event in stable_events:
            stream.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return len(stable_events)


def export_trajectory(
    *,
    session_id: str,
    terminal_status: str,
    sessions_root: Path,
    output_root: Path,
    api_url: str,
    credentials_dir: Path,
) -> int:
    uuid.UUID(session_id)
    output_root.mkdir(parents=True, exist_ok=True)
    manifest_path = output_root / "manifest.json"
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "session_id": session_id,
        "product_terminal_status": terminal_status,
        "failed": terminal_status != "completed",
        "capture_status": "started",
        "server_session_saved": False,
        "server_session_sha256": None,
        "server_events_saved": False,
        "server_event_count": 0,
        "server_events_sha256": None,
        "local_file_count": 0,
        "local_trace_file_count": 0,
        "tool_result_file_count": 0,
        "local_journal_saved": False,
        "local_journal_path": None,
        "local_journal_sha256": None,
        "local_journal_event_count": 0,
        "local_journal_terminal_event": None,
        "local_files": [],
        "errors": [],
    }
    _write_json(manifest_path, manifest)

    try:
        manifest["local_files"] = _copy_local_session(
            sessions_root,
            session_id,
            output_root,
        )
        manifest["local_file_count"] = len(manifest["local_files"])
        manifest["local_trace_file_count"] = sum(
            1
            for value in manifest["local_files"]
            if value["bytes"] > 0
            and (
                value["path"].endswith(f"/{session_id}.jsonl")
                or value["path"].endswith("/step_events.jsonl")
            )
        )
        manifest["tool_result_file_count"] = sum(
            1
            for value in manifest["local_files"]
            if "/tool-results/" in f"/{value['path']}"
        )
        journals = [
            value
            for value in manifest["local_files"]
            if value["path"].endswith(f"/{session_id}.jsonl")
        ]
        if not journals:
            raise RuntimeError("local Astra session journal was not found")
        journal = next(
            (
                value
                for value in journals
                if "/v1/users/" in f"/{value['path']}"
            ),
            journals[0],
        )
        journal_summary = _validate_local_journal(
            output_root / journal["path"],
            session_id,
        )
        manifest["local_journal_saved"] = True
        manifest["local_journal_path"] = journal["path"]
        manifest["local_journal_sha256"] = journal["sha256"]
        manifest["local_journal_event_count"] = journal_summary[
            "event_count"
        ]
        manifest["local_journal_terminal_event"] = journal_summary[
            "terminal_event"
        ]
    except Exception as exc:
        manifest["errors"].append(
            {"source": "local_session", "error": type(exc).__name__}
        )

    try:
        token = _load_access_token(credentials_dir)
        session = _get_json(
            api_url,
            token,
            f"/sessions/{urllib.parse.quote(session_id, safe='')}",
        )
        if (
            not isinstance(session, dict)
            or session.get("session_id") != session_id
        ):
            raise RuntimeError(
                "session response does not match the requested session"
            )
        server_session_path = output_root / "server-session.json"
        _write_json(server_session_path, session)
        manifest["server_session_saved"] = True
        manifest["server_session_sha256"] = _sha256(server_session_path)
        server_events_path = output_root / "server-events.jsonl"
        manifest["server_event_count"] = _export_server_events(
            api_url,
            token,
            session_id,
            server_events_path,
        )
        manifest["server_events_saved"] = True
        manifest["server_events_sha256"] = _sha256(server_events_path)
    except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
        manifest["errors"].append(
            {"source": "server_api", "error": type(exc).__name__}
        )

    local_journal_complete = manifest["local_journal_saved"] and (
        terminal_status != "completed"
        or manifest["local_journal_terminal_event"] == "session_end"
    )
    if (
        manifest["server_session_saved"]
        and manifest["server_events_saved"]
        and manifest["server_event_count"] > 0
        and local_journal_complete
    ):
        manifest["capture_status"] = "complete"
    elif (
        manifest["server_session_saved"]
        or manifest["server_events_saved"]
        or manifest["local_journal_saved"]
        or manifest["local_trace_file_count"] > 0
    ):
        manifest["capture_status"] = "partial"
    else:
        manifest["capture_status"] = "missing"
    _write_json(manifest_path, manifest)
    return 0 if manifest["capture_status"] == "complete" else 3


def main() -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register")
    register.add_argument("--controller-run-id", required=True)
    register.add_argument("--task-id", required=True)
    discover = commands.add_parser("discover")
    discover.add_argument("--sessions-root", type=Path, required=True)
    export = commands.add_parser("export")
    export.add_argument("--session-id", required=True)
    export.add_argument("--terminal-status", required=True)
    export.add_argument("--sessions-root", type=Path, required=True)
    export.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    api_url = os.environ.get("ASTRA_API_URL", "")
    credentials_dir = os.environ.get("ASTRA_CLI_CREDENTIALS_DIR", "")
    if not api_url or not credentials_dir:
        print("Astra API URL or credentials directory is missing", file=sys.stderr)
        return 2
    try:
        if args.command == "discover":
            print(json.dumps({"session_id": discover_session_id(args.sessions_root)}))
            return 0
        if args.command == "register":
            value = register_session(
                api_url=api_url,
                credentials_dir=Path(credentials_dir),
                controller_run_id=args.controller_run_id,
                task_id=args.task_id,
            )
            print(json.dumps(value, ensure_ascii=False, sort_keys=True))
            return 0
        return export_trajectory(
            session_id=args.session_id,
            terminal_status=args.terminal_status,
            sessions_root=args.sessions_root,
            output_root=args.output_dir,
            api_url=api_url,
            credentials_dir=Path(credentials_dir),
        )
    except Exception as exc:
        print(f"trajectory export failed: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
