"""Append-only audit log (audit.jsonl) for headless runs and local sessions."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path

_SECRET_KEYS = {
    "authorization",
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "client_secret",
    "secret",
    "password",
    "api_key",
    "apikey",
    "sas",
    "sig",
}


def _redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in _SECRET_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


class AuditLog:
    def __init__(self, path: Path | None = None, actor: str | None = None):
        self.path = path
        self.actor = actor
        self.records: list[dict] = []
        self._lock = threading.Lock()

    def write(self, event: str, **fields) -> dict:
        rec = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "event": event, **_redact(fields)}
        with self._lock:
            self.records.append(rec)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, default=str) + "\n")
        return rec
