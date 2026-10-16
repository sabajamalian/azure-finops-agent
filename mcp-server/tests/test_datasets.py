"""DatasetStore: storage, SELECT-only querying, truncation, column names."""

from __future__ import annotations

import pytest

from azure_finops.datasets import MAX_RESULT_ROWS, DatasetStore
from azure_finops.errors import ValidationFailed

ROWS = [
    {"ServiceName": "Virtual Machines", "cost": 120.5, "currency": "USD"},
    {"ServiceName": "Storage", "cost": 10.25, "currency": "USD"},
    {"ServiceName": "Log Analytics", "cost": 42.0, "currency": "EUR"},
]


@pytest.fixture
def store():
    s = DatasetStore()
    s.add(ROWS, "costs by service", evidence_id="ev-001", scope="/subscriptions/x")
    return s


def test_add_records_metadata(store):
    info = store.info("ds_1")
    assert info.table == "ds_1"
    assert info.columns == ["ServiceName", "cost", "currency"]
    assert info.row_count == 3
    assert info.description == "costs by service"
    assert info.evidence_id == "ev-001"
    assert info.meta == {"scope": "/subscriptions/x"}
    second = store.add([{"a": 1}], "other")
    assert second.id == "ds_2"
    assert [d.id for d in store.list()] == ["ds_1", "ds_2"]


def test_unknown_dataset(store):
    with pytest.raises(ValidationFailed, match="ds_1"):
        store.info("ds_9")


def test_select_and_aggregate(store):
    cols, rows, truncated = store.query(
        "SELECT currency, round(sum(cost), 2) AS total FROM ds_1 GROUP BY currency ORDER BY currency"
    )
    assert cols == ["currency", "total"]
    assert rows == [["EUR", 42.0], ["USD", 130.75]]
    assert truncated is False


def test_with_cte_and_trailing_semicolon(store):
    cols, rows, _ = store.query(
        "with big as (select * from ds_1 where cost > 40) select ServiceName from big order by cost desc;"
    )
    assert cols == ["ServiceName"]
    assert rows == [["Virtual Machines"], ["Log Analytics"]]


def test_rows_missing_keys_and_nested_values():
    s = DatasetStore()
    s.add([{"a": 1, "b": {"x": 1}}, {"c": "only-c"}, {"a": True, "b": None}], "mixed")
    cols, rows, _ = s.query("select a, b, c from ds_1")
    assert cols == ["a", "b", "c"]
    assert rows == [[1, "{'x': 1}", None], [None, None, "only-c"], [1, None, None]]


def test_empty_dataset_is_queryable():
    s = DatasetStore()
    info = s.add([], "nothing")
    assert info.row_count == 0
    assert s.query("select count(*) from ds_1")[1] == [[0]]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO ds_1 VALUES ('x', 1, 'USD')",
        "UPDATE ds_1 SET cost = 0",
        "DELETE FROM ds_1",
        "DROP TABLE ds_1",
        "ATTACH DATABASE 'other.db' AS other",
        "DETACH DATABASE main",
        "PRAGMA table_info(ds_1)",
        "CREATE TABLE t (a)",
        "ALTER TABLE ds_1 ADD COLUMN x",
        "REPLACE INTO ds_1 VALUES ('x', 1, 'USD')",
        "VACUUM",
        "  -- comment\nSELECT 1",
        "/* hi */ SELECT 1",
        "EXPLAIN SELECT 1",
        "selectx * from ds_1",
    ],
)
def test_non_select_statements_rejected(store, sql):
    with pytest.raises(ValidationFailed, match="only SELECT"):
        store.query(sql)
    assert store.query("select count(*) from ds_1")[1] == [[3]]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; DROP TABLE ds_1",
        "select * from ds_1; delete from ds_1;",
        "SELECT 1;SELECT 2",
        "SELECT * FROM ds_1 -- ; DROP TABLE ds_1",
    ],
)
def test_multiple_statements_rejected(store, sql):
    with pytest.raises(ValidationFailed, match="one SQL statement"):
        store.query(sql)
    assert store.query("select count(*) from ds_1")[1] == [[3]]


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) INSERT INTO ds_1 (cost) SELECT * FROM x",
        "with x as (select 1) delete from ds_1",
        "WITH x AS (SELECT 1) UPDATE ds_1 SET cost = 0",
        "select * from pragma_table_info('ds_1')",
        "select load_extension('evil')",
    ],
)
def test_writes_disguised_as_select_are_denied_by_authorizer(store, sql):
    with pytest.raises(ValidationFailed, match="SQL error"):
        store.query(sql)
    assert store.query("select round(sum(cost), 2) from ds_1")[1] == [[172.75]]


def test_sql_errors_are_validation_failures(store):
    with pytest.raises(ValidationFailed, match="no such table"):
        store.query("select * from ds_99")
    with pytest.raises(ValidationFailed, match="SQL error"):
        store.query("select nope from ds_1")


def test_authorizer_is_reset_after_query(store):
    with pytest.raises(ValidationFailed):
        store.query("with x as (select 1) delete from ds_1")
    info = store.add([{"z": 1}], "added after a denied query")
    assert store.query(f"select z from {info.table}")[1] == [[1]]


def test_truncation_at_max_rows():
    s = DatasetStore()
    s.add([{"i": i} for i in range(20)], "twenty")
    cols, rows, truncated = s.query("select i from ds_1 order by i", max_rows=5)
    assert rows == [[0], [1], [2], [3], [4]] and truncated is True
    _, rows, truncated = s.query("select i from ds_1", max_rows=20)
    assert len(rows) == 20 and truncated is False


def test_default_max_rows():
    s = DatasetStore()
    s.add([{"i": i} for i in range(MAX_RESULT_ROWS + 10)], "many")
    _, rows, truncated = s.query("select i from ds_1")
    assert len(rows) == MAX_RESULT_ROWS and truncated is True


def test_column_names_are_sanitized_and_deduplicated():
    s = DatasetStore()
    info = s.add(
        [{"cost (USD)": 1, "tag:env": "prod", "1st": "a", "a b": 2, "a_b": 3, "": 4, '"; drop': 5}],
        "odd names",
    )
    assert info.columns == ["cost__USD", "tag_env", "c_1st", "a_b", "a_b_", "col", "drop"]
    cols, rows, _ = s.query('select cost__USD, tag_env, c_1st, a_b, a_b_, col, "drop" from ds_1')
    assert rows == [[1, "prod", "a", 2, 3, 4, 5]]
