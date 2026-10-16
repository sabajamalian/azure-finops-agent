"""EvidenceLog ids and persistence; AuditLog redaction and persistence."""

from __future__ import annotations

import json

from azure_finops.audit import AuditLog
from azure_finops.evidence import Evidence, EvidenceLog, stable_hash


def test_evidence_ids_are_sequential_and_hash_query():
    log = EvidenceLog()
    a = log.new(
        tool="query_costs", source="Cost Management query", scope=["/subscriptions/x"], query={"b": 1, "a": 2}
    )
    b = log.new(tool="query_costs", source="Cost Management query", scope=["/subscriptions/x"])
    c = log.add(Evidence(id="ignored", tool="t", source="s", scope=[]))
    assert [a.id, b.id, c.id] == ["ev-001", "ev-002", "ev-003"]
    assert a.query_hash == stable_hash({"a": 2, "b": 1})
    assert b.query_hash is None
    assert log.ids() == {"ev-001", "ev-002", "ev-003"}
    assert log.get("ev-002") is b and log.get("ev-404") is None
    assert [e.id for e in log.all()] == ["ev-001", "ev-002", "ev-003"]


def test_stable_hash_is_order_insensitive_and_short():
    h = stable_hash({"x": [1, 2], "y": {"b": 1, "a": 2}})
    assert h == stable_hash({"y": {"a": 2, "b": 1}, "x": [1, 2]})
    assert h != stable_hash({"x": [2, 1], "y": {"a": 2, "b": 1}})
    assert len(h) == 16


def test_explicit_query_hash_is_kept():
    log = EvidenceLog()
    ev = log.new(tool="t", source="s", scope=[], query={"q": 1}, query_hash="custom")
    assert ev.query_hash == "custom"


def test_summary_omits_query_and_empty_fields():
    log = EvidenceLog()
    ev = log.new(
        tool="query_costs",
        source="Cost Management query",
        scope=["/subscriptions/x"],
        period_from="2026-03-01",
        currency=["USD"],
        query={"body": "large"},
    )
    s = ev.summary()
    assert "query" not in s
    assert s["query_hash"] == ev.query_hash
    assert s["id"] == "ev-001" and s["currency"] == ["USD"] and s["period_from"] == "2026-03-01"
    for empty in ("period_to", "granularity", "request_ids", "notes", "data_through"):
        assert empty not in s
    assert s["partial_period"] is False and s["row_count"] == 0


def test_evidence_persists_and_reloads(tmp_path):
    path = tmp_path / "out" / "evidence.jsonl"
    log = EvidenceLog(path)
    log.new(tool="a", source="s", scope=["/subscriptions/x"], notes=["n1"], request_ids=["r1"])
    log.new(tool="b", source="s", scope=[], query={"k": "v"})
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["id"] for x in lines] == ["ev-001", "ev-002"]
    assert lines[1]["query"] == {"k": "v"}

    reloaded = EvidenceLog(path)
    assert reloaded.ids() == {"ev-001", "ev-002"}
    first = reloaded.get("ev-001")
    assert first.tool == "a" and first.notes == ["n1"] and first.request_ids == ["r1"]
    assert reloaded.get("ev-002").query_hash == log.get("ev-002").query_hash
    nxt = reloaded.new(tool="c", source="s", scope=[])
    assert nxt.id == "ev-003"
    assert len(path.read_text().splitlines()) == 3


def test_evidence_without_path_writes_nothing(tmp_path):
    log = EvidenceLog(None)
    log.new(tool="a", source="s", scope=[])
    assert list(tmp_path.iterdir()) == []


def test_audit_redacts_secret_like_keys(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl", actor="ci")
    original = {
        "Authorization": "Bearer abc",
        "nested": {"token": "t1", "Password": "p", "keep": "visible"},
        "items": [{"client_secret": "cs"}, {"access_token": "at", "path": "/subscriptions"}],
    }
    rec = audit.write("arm_request", method="GET", headers=original, status=200)
    assert rec["event"] == "arm_request" and rec["method"] == "GET" and rec["status"] == 200
    h = rec["headers"]
    assert h["Authorization"] == "***"
    assert h["nested"] == {"token": "***", "Password": "***", "keep": "visible"}
    assert h["items"] == [{"client_secret": "***"}, {"access_token": "***", "path": "/subscriptions"}]
    assert original["Authorization"] == "Bearer abc"  # caller's dict is not mutated

    text = (tmp_path / "audit.jsonl").read_text()
    for secret in ("Bearer abc", "t1", '"p"', "cs", '"at"'):
        assert secret not in text
    assert json.loads(text.splitlines()[0])["headers"]["nested"]["keep"] == "visible"


def test_top_level_secret_fields_are_redacted():
    audit = AuditLog()
    rec = audit.write("login", token="xyz", password="hunter2", user="alice")
    assert rec["token"] == "***" and rec["password"] == "***" and rec["user"] == "alice"


def test_audit_appends_records_with_timestamps(tmp_path):
    path = tmp_path / "deep" / "audit.jsonl"
    audit = AuditLog(path)
    audit.write("a", n=1)
    audit.write("b", n=2, obj=object())  # non-JSON values are stringified, not fatal
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["event"] for x in lines] == ["a", "b"]
    assert all(x["ts"].endswith("+00:00") for x in lines)
    assert len(audit.records) == 2
