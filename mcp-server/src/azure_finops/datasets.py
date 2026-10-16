"""Session-local dataset store. Large results land here; the model queries subsets with SELECT-only SQL."""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field

from .errors import ValidationFailed

_ALLOWED_ACTIONS = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION}
_COL_RE = re.compile(r"[^A-Za-z0-9_]")
MAX_RESULT_ROWS = 500


@dataclass
class DatasetInfo:
    id: str
    table: str
    columns: list[str]
    row_count: int
    description: str
    evidence_id: str | None = None
    meta: dict = field(default_factory=dict)


def _col(name: str) -> str:
    c = _COL_RE.sub("_", name).strip("_") or "col"
    return f"c_{c}" if c[0].isdigit() else c


class DatasetStore:
    def __init__(self):
        self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._lock = threading.Lock()
        self._info: dict[str, DatasetInfo] = {}

    def add(self, rows: list[dict], description: str, evidence_id: str | None = None, **meta) -> DatasetInfo:
        cols: list[str] = []
        for r in rows:
            for k in r:
                if k not in cols:
                    cols.append(k)
        safe = []
        for c in cols:
            s = _col(c)
            while s in safe:
                s += "_"
            safe.append(s)
        with self._lock:
            ds_id = f"ds_{len(self._info) + 1}"
            table = ds_id
            col_sql = ", ".join(f'"{c}"' for c in safe) or '"empty"'
            self._db.execute(f'CREATE TABLE "{table}" ({col_sql})')
            if rows and safe:
                ph = ", ".join("?" for _ in safe)
                self._db.executemany(
                    f'INSERT INTO "{table}" VALUES ({ph})',
                    [tuple(_cell(r.get(c)) for c in cols) for r in rows],
                )
            self._db.commit()
            info = DatasetInfo(ds_id, table, safe, len(rows), description, evidence_id, meta)
            self._info[ds_id] = info
        return info

    def info(self, ds_id: str) -> DatasetInfo:
        if ds_id not in self._info:
            raise ValidationFailed(f"unknown dataset {ds_id!r}; available: {', '.join(self._info) or 'none'}")
        return self._info[ds_id]

    def list(self) -> list[DatasetInfo]:
        return list(self._info.values())

    def query(self, sql: str, max_rows: int = MAX_RESULT_ROWS) -> tuple[list[str], list[list], bool]:
        stmt = sql.strip().rstrip(";")
        if ";" in stmt:
            raise ValidationFailed("only one SQL statement is allowed")
        if not re.match(r"^(select|with)\b", stmt, re.I):
            raise ValidationFailed("only SELECT queries are allowed")

        def authorizer(action, *_):
            return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY

        with self._lock:
            self._db.set_authorizer(authorizer)
            try:
                cur = self._db.execute(stmt)
                cols = [d[0] for d in cur.description or []]
                rows = cur.fetchmany(max_rows + 1)
            except sqlite3.DatabaseError as exc:
                raise ValidationFailed(f"SQL error: {exc}") from exc
            finally:
                self._db.set_authorizer(None)
        truncated = len(rows) > max_rows
        return cols, [list(r) for r in rows[:max_rows]], truncated


def _cell(v):
    if v is None or isinstance(v, (int, float, str)):
        return v
    return str(v)
