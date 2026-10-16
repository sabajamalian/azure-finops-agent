"""Evidence envelopes: every numeric tool result is registered here and cited by id."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path


def stable_hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]


@dataclass
class Evidence:
    id: str
    tool: str
    source: str
    scope: list[str]
    period_from: str | None = None
    period_to: str | None = None
    granularity: str | None = None
    cost_basis: str | None = None
    currency: list[str] = field(default_factory=list)
    data_through: str | None = None
    partial_period: bool = False
    row_count: int = 0
    request_ids: list[str] = field(default_factory=list)
    query: dict | None = None
    query_hash: str | None = None
    notes: list[str] = field(default_factory=list)
    retrieved_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))

    def summary(self) -> dict:
        """Compact form returned to the model (the full query stays in the evidence log)."""
        d = asdict(self)
        d.pop("query", None)
        return {k: v for k, v in d.items() if v not in (None, [], "")}


class EvidenceLog:
    def __init__(self, path: Path | None = None):
        self._items: dict[str, Evidence] = {}
        self._lock = threading.Lock()
        self._path = path
        if path and path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    ev = Evidence(**json.loads(line))
                    self._items[ev.id] = ev

    def add(self, ev: Evidence) -> Evidence:
        with self._lock:
            ev.id = f"ev-{len(self._items) + 1:03d}"
            if ev.query is not None and ev.query_hash is None:
                ev.query_hash = stable_hash(ev.query)
            self._items[ev.id] = ev
            if self._path:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(asdict(ev), default=str) + "\n")
        return ev

    def new(self, **kwargs) -> Evidence:
        return self.add(Evidence(id="", **kwargs))

    def get(self, ev_id: str) -> Evidence | None:
        return self._items.get(ev_id)

    def ids(self) -> set[str]:
        return set(self._items)

    def all(self) -> list[Evidence]:
        return list(self._items.values())
