import asyncio

from conftest import PROD_SCOPE

import azure_finops.server as srv


def test_all_tools_registered_with_annotations():
    tools = {t.name: t for t in asyncio.run(srv.server.list_tools())}
    assert len(tools) == 16
    for name, t in tools.items():
        ann = t.annotations
        assert ann is not None and ann.destructive_hint is False
        assert ann.read_only_hint is (name != "submit_result")


def test_query_costs_tool_returns_evidence(rt):
    out = srv.query_costs(
        scope=PROD_SCOPE, start_date="2026-03-01", end_date="2026-03-10", group_by=["ServiceName"]
    )
    assert out["status"] == "ok"
    assert out["evidence_id"] in rt.evidence.ids()


def test_errors_become_structured_results(rt):
    out = srv.query_costs(
        scope="/subscriptions/99999999-9999-9999-9999-999999999999",
        start_date="2026-03-01",
        end_date="2026-03-02",
    )
    assert out["status"] in ("out_of_scope", "invalid")
