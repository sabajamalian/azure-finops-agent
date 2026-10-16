"""Config model validation, loading, and scope parsing."""

from __future__ import annotations

import copy

import pytest
import yaml
from pydantic import ValidationError

from azure_finops.config import (
    Config,
    ConfigError,
    config_path,
    load_config,
    parse_scope,
    repo_root,
)

TENANT = "00000000-0000-0000-0000-0000000000aa"
SUB_A = "AAAAAAAA-1111-2222-3333-444444444444"
SUB_B = "bbbbbbbb-1111-2222-3333-444444444444"

VALID = {
    "version": 1,
    "agent": {"name": "finops-bot"},
    "azure": {
        "tenant_id": TENANT,
        "subscriptions": [{"id": SUB_A, "name": "prod"}, {"id": SUB_B}],
        "cost_basis": "AmortizedCost",
    },
    "timezone": "America/Los_Angeles",
    "anomaly": {"lookback_days": 45, "group_by": ["ServiceName", "tag:team"]},
    "destinations": [{"id": "finops-team", "labels": ["finops"]}, {"id": "platform"}],
}


def cfg(**changes):
    data = copy.deepcopy(VALID)
    for dotted, value in changes.items():
        node = data
        *parents, leaf = dotted.split("__")
        for p in parents:
            node = node[p]
        node[leaf] = value
    return data


def test_valid_config_and_defaults():
    c = Config.model_validate(VALID)
    assert c.subscription_ids() == [SUB_A.lower(), SUB_B]
    assert c.subscription_name(SUB_A) == "prod"
    assert c.subscription_name(SUB_B) == SUB_B  # no friendly name -> id
    assert c.subscription_name("unknown") == "unknown"
    assert c.azure.cost_basis == "AmortizedCost"
    assert c.destination("platform").type == "github_issue"
    assert c.destination("nope") is None
    assert c.anomaly.lookback_days == 45 and c.anomaly.baseline_days == 28 and c.anomaly.min_impact == 50.0
    assert c.retention_days == 30 and c.max_issues_per_run == 5 and c.allow_public_repo_output is False


def test_minimal_config_uses_defaults():
    c = Config.model_validate(
        {"azure": {"tenant_id": TENANT, "subscriptions": [{"id": SUB_B}]}, "destinations": [{"id": "x"}]}
    )
    assert c.timezone == "UTC" and c.azure.cost_basis == "ActualCost"
    assert c.anomaly.group_by == ["ResourceGroupName", "ServiceName"]
    assert c.agent.name == "azure-finops"


@pytest.mark.parametrize(
    "data",
    [
        cfg(unknown_top_level=True),
        cfg(azure__extra="x"),
        cfg(agent__colour="blue"),
        cfg(anomaly__threshold=3),
        cfg(destinations=[{"id": "finops-team", "channel": "#x"}]),
        cfg(azure__subscriptions=[{"id": SUB_A, "nickname": "p"}]),
    ],
    ids=["top", "azure", "agent", "anomaly", "destination", "subscription"],
)
def test_extra_keys_rejected(data):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Config.model_validate(data)


def test_duplicate_subscriptions_rejected_case_insensitively():
    with pytest.raises(ValidationError, match="duplicate subscription"):
        Config.model_validate(cfg(azure__subscriptions=[{"id": SUB_A}, {"id": SUB_A.lower()}]))


def test_duplicate_destinations_rejected():
    with pytest.raises(ValidationError, match="duplicate destination"):
        Config.model_validate(cfg(destinations=[{"id": "a"}, {"id": "a", "labels": ["x"]}]))


@pytest.mark.parametrize("tz", ["Mars/Olympus", "PST8PDT-nope", "", "../etc/passwd"])
def test_bad_timezone_rejected(tz):
    with pytest.raises(ValidationError, match="unknown timezone"):
        Config.model_validate(cfg(timezone=tz))


@pytest.mark.parametrize(
    ("dotted", "value"),
    [
        ("azure__tenant_id", "not-a-guid"),
        ("azure__subscriptions", []),
        ("azure__subscriptions", [{"id": "1234"}]),
        ("azure__cost_basis", "Usage"),
        ("version", 2),
        ("destinations", []),
        ("destinations", [{"id": "Has_Caps"}]),
        ("destinations", [{"id": "-leading-dash"}]),
        ("destinations", [{"id": "x", "type": "slack"}]),
        ("anomaly__group_by", ["ServiceName", "ResourceGroupName", "Meter"]),
        ("anomaly__group_by", ["NotADimension"]),
        ("anomaly__lookback_days", 7),
        ("anomaly__skip_recent_days", 8),
        ("anomaly__z_threshold", 0),
        ("retention_days", 0),
        ("max_issues_per_run", 26),
    ],
)
def test_invalid_values_rejected(dotted, value):
    with pytest.raises(ValidationError):
        Config.model_validate(cfg(**{dotted: value}))


def test_load_config_from_repo(repo):
    (repo / "finops" / "config.yaml").write_text(yaml.safe_dump(VALID))
    assert repo_root() == repo.resolve()
    assert config_path() == repo.resolve() / "finops" / "config.yaml"
    c = load_config()
    assert c.timezone == "America/Los_Angeles"


def test_load_config_env_override(repo, monkeypatch, tmp_path):
    alt = tmp_path / "elsewhere.yaml"
    alt.write_text(yaml.safe_dump(cfg(timezone="Europe/Berlin")))
    monkeypatch.setenv("FINOPS_CONFIG", str(alt))
    assert config_path() == alt.resolve()
    assert load_config().timezone == "Europe/Berlin"


def test_load_config_missing_file_points_to_example(repo):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        load_config()


def test_load_config_wraps_validation_errors(repo):
    p = repo / "finops" / "config.yaml"
    p.write_text(yaml.safe_dump(cfg(surprise=1)))
    with pytest.raises(ConfigError, match="config.yaml"):
        load_config()
    p.write_text("")
    with pytest.raises(ConfigError, match="azure"):
        load_config()


def test_repo_root_walks_up_without_env(tmp_path, monkeypatch):
    monkeypatch.delenv("FINOPS_REPO_ROOT", raising=False)
    (tmp_path / "finops").mkdir()
    (tmp_path / ".git").mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert repo_root(nested) == tmp_path.resolve()
    lone = tmp_path / "a"
    (tmp_path / ".git").rmdir()
    assert repo_root(lone) == lone.resolve()  # no marker found -> start dir


# ---- parse_scope -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (SUB_A, (SUB_A.lower(), None)),
        (f"  {SUB_A}  ", (SUB_A.lower(), None)),
        (f"/subscriptions/{SUB_A}", (SUB_A.lower(), None)),
        (f"/subscriptions/{SUB_A}/", (SUB_A.lower(), None)),
        (f"/SUBSCRIPTIONS/{SUB_A}", (SUB_A.lower(), None)),
        (f"/subscriptions/{SUB_B}/resourceGroups/rg-web", (SUB_B, "rg-web")),
        (f"/subscriptions/{SUB_B}/resourcegroups/RG_Data.v2(old)/", (SUB_B, "RG_Data.v2(old)")),
        (f"/subscriptions/{SUB_B}/resourceGroups/{'r' * 90}", (SUB_B, "r" * 90)),
        (f"/subscriptions/{SUB_B}/resourceGroups/a", (SUB_B, "a")),
        (f"/subscriptions/{SUB_B}/resourceGroups/rg.x", (SUB_B, "rg.x")),
    ],
)
def test_parse_scope_valid(scope, expected):
    assert parse_scope(scope) == expected


@pytest.mark.parametrize(
    "scope",
    [
        "",
        "prod",
        "1234",
        "subscriptions/" + SUB_A,
        "/subscriptions/not-a-guid",
        "/subscriptions/" + "-" * 36,
        "/subscriptions/" + "a" * 36,
        f"/subscriptions/{SUB_A}x",
        f"/subscriptions/{SUB_A}/providers/Microsoft.Compute/virtualMachines/vm1",
        f"/subscriptions/{SUB_A}/resourceGroups/rg/providers/Microsoft.Compute/virtualMachines/vm1",
        f"/subscriptions/{SUB_A}/resourceGroups/",
        f"/subscriptions/{SUB_A}/resourceGroups/rg/../../other",
        f"/subscriptions/{SUB_A}/../{SUB_B}",
        f"/subscriptions/{SUB_A}/resourceGroups/rg?api-version=1",
        f"/providers/Microsoft.Management/managementGroups/mg1/subscriptions/{SUB_A}",
        f"https://management.azure.com/subscriptions/{SUB_A}",
    ],
)
def test_parse_scope_rejects_unsupported_shapes(scope):
    with pytest.raises(ValueError, match="unsupported scope|invalid resource group"):
        parse_scope(scope)


@pytest.mark.parametrize(
    "rg",
    [
        "..",
        ".",
        "rg.",
        "rg\nx",
        "rg name",
        "rg;rm",
        "rg'or'1",
        "rg%2F..",
        "rg*",
        "r" * 91,
    ],
)
def test_parse_scope_rejects_invalid_resource_group_names(rg):
    with pytest.raises(ValueError, match="invalid resource group|unsupported scope"):
        parse_scope(f"/subscriptions/{SUB_A}/resourceGroups/{rg}")
    with pytest.raises(ValueError, match="invalid resource group|unsupported scope"):
        parse_scope(f"/subscriptions/{SUB_A}/resourceGroups/{rg}/")


def test_parse_scope_newline_before_slash_is_not_accepted():
    # `$` in a regex matches before a trailing newline; parse_scope must not rely on that.
    with pytest.raises(ValueError, match="invalid resource group"):
        parse_scope(f"/subscriptions/{SUB_A}/resourceGroups/rg\n/")
    # surrounding whitespace is stripped, so a trailing newline on the whole scope is fine
    assert parse_scope(f"/subscriptions/{SUB_A}/resourceGroups/rg\n") == (SUB_A.lower(), "rg")
