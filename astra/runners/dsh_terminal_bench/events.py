from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .driver import ProtocolError, summarize_events


def validate_event_stream(
    path: Path,
    *,
    session_id: str,
    max_turns: int | None = None,
) -> dict[str, Any]:
    """Validate a persisted DSH JSON-RPC wire stream for one root session."""

    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"cannot read DSH event stream {path}: {exc}") from exc
    rows: list[dict[str, Any]] = []
    try:
        for line in raw.decode("utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise RuntimeError("DSH event stream rows must be JSON objects")
            rows.append(row)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("DSH event stream is not valid JSONL") from exc
    if not rows:
        raise RuntimeError("DSH event stream is empty")

    events: list[dict[str, Any]] = []
    root_idle_seen = False
    for row in rows:
        params = row.get("params")
        if not isinstance(params, dict) or params.get("sessionId") != session_id:
            continue
        if row.get("method") == "session.event":
            event = params.get("event")
            if not isinstance(event, dict):
                raise RuntimeError("DSH session.event has no event object")
            events.append(event)
        elif (
            row.get("method") == "session.status"
            and params.get("status") == "idle"
        ):
            root_idle_seen = True
    if not root_idle_seen:
        raise RuntimeError("DSH event stream has no root-session idle status")
    try:
        summary = summarize_events(events, session_id, max_turns=max_turns)
    except ProtocolError as exc:
        raise RuntimeError(f"invalid DSH native event: {exc}") from exc
    return {
        **summary,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "wire_message_count": len(rows),
    }
