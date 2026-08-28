#!/usr/bin/env python3
"""Refresh the local MaxKB admin token without printing credentials."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SECRET_DIR = ROOT / ".local-services/maxkb_local/secrets"


def main() -> int:
    base = (os.getenv("MAXKB_BASE_URL", "").strip() or "http://127.0.0.1:8090").rstrip("/")
    request_path = Path(
        os.getenv("MAXKB_LOGIN_REQUEST_FILE", str(SECRET_DIR / "login-request.json"))
    )
    token_path = Path(
        os.getenv("MAXKB_ADMIN_TOKEN_FILE", str(SECRET_DIR / "admin.token"))
    )
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    request = urllib.request.Request(
        f"{base}/admin/api/user/login",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"MAXKB_LOGIN_REFRESH_HTTP_{exc.code}") from exc
    token = str((result.get("data") or {}).get("token") or "")
    if result.get("code") != 200 or not token:
        raise SystemExit("MAXKB_LOGIN_REFRESH_INVALID_RESPONSE")
    token_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    token_path.write_text(token + "\n", encoding="utf-8")
    token_path.chmod(0o600)
    print("MaxKB admin token refreshed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
