import io

import pytest
import yaml

from azure_finops import cli
from azure_finops import init as init_mod
from azure_finops.config import load_config
from azure_finops.init import InitError, choose, init_config, render_config

TENANT = "aaaaaaaa-1111-2222-3333-444444444444"
OTHER = "bbbbbbbb-1111-2222-3333-444444444444"
SUBS = [
    {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "contoso-prod",
        "tenantId": TENANT,
        "state": "Enabled",
    },
    {
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "contoso-dev",
        "tenantId": TENANT,
        "state": "Enabled",
    },
    {"id": "33333333-3333-3333-3333-333333333333", "name": "old", "tenantId": TENANT, "state": "Disabled"},
    {
        "id": "44444444-4444-4444-4444-444444444444",
        "name": "elsewhere",
        "tenantId": OTHER,
        "state": "Enabled",
    },
]
EXAMPLE = (init_mod.Path(__file__).resolve().parents[2] / "finops" / "config.example.yaml").read_text()


def fake_az(args):
    if args[:2] == ["account", "show"]:
        return {"tenantId": TENANT.upper()}
    if args[:2] == ["account", "list"]:
        return SUBS
    raise AssertionError(args)


@pytest.fixture
def root(repo):
    (repo / "finops" / "config.example.yaml").write_text(EXAMPLE)
    return repo


def test_init_all_keeps_example_comments(root):
    path = init_config(root, all_subs=True, timezone="Europe/London", az=fake_az)
    text = path.read_text()
    assert "# Where headless runs may post." in text
    cfg = load_config(path)
    assert cfg.azure.tenant_id == TENANT
    assert cfg.subscription_ids() == [SUBS[0]["id"], SUBS[1]["id"]]
    assert cfg.subscription_name(SUBS[1]["id"]) == "contoso-dev"
    assert cfg.timezone == "Europe/London"
    assert "00000000-0000" not in text


def test_init_picks_by_name_id_or_number(root):
    path = init_config(root, picks=["contoso-dev", SUBS[0]["id"].upper()], timezone="UTC", az=fake_az)
    assert load_config(path).subscription_ids() == [SUBS[1]["id"], SUBS[0]["id"]]
    assert [s["name"] for s in choose(SUBS[:2], ["2", "1", "2"])] == ["contoso-dev", "contoso-prod"]


def test_init_interactive(root):
    out = io.StringIO()
    path = init_config(root, az=fake_az, ask=lambda prompt: "1", timezone="UTC", out=out)
    assert "contoso-prod" in out.getvalue() and "elsewhere" not in out.getvalue()
    assert "old" not in out.getvalue()
    assert load_config(path).subscription_ids() == [SUBS[0]["id"]]


def test_init_explicit_tenant(root):
    path = init_config(root, tenant=OTHER, all_subs=True, timezone="UTC", az=fake_az)
    assert load_config(path).subscription_ids() == [SUBS[3]["id"]]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "pass --subscription"),
        ({"picks": ["nope"]}, "no subscription matches 'nope'"),
        ({"tenant": "cccccccc-1111-2222-3333-444444444444", "all_subs": True}, "no enabled subscriptions"),
        ({"all_subs": True, "timezone": "Mars/Base"}, "unknown time zone"),
    ],
)
def test_init_errors(root, kwargs, message):
    with pytest.raises(InitError, match=message):
        init_config(root, az=fake_az, **kwargs)
    assert not (root / "finops" / "config.yaml").exists()


def test_init_refuses_to_overwrite(root):
    (root / "finops" / "config.yaml").write_text("keep: me\n")
    with pytest.raises(InitError, match="--force"):
        init_config(root, all_subs=True, az=fake_az)
    init_config(root, all_subs=True, force=True, timezone="UTC", az=fake_az)
    assert "keep: me" not in (root / "finops" / "config.yaml").read_text()


def test_render_without_example_falls_back_to_minimal_yaml():
    text = render_config(None, TENANT, SUBS[:1], "UTC")
    data = yaml.safe_load(text)
    assert data["azure"]["subscriptions"] == [{"id": SUBS[0]["id"], "name": "contoso-prod"}]
    assert data["timezone"] == "UTC"


def test_render_quotes_odd_names():
    text = render_config(EXAMPLE, TENANT, [{"id": SUBS[0]["id"], "name": 'Prod: "main" #1'}], None)
    assert yaml.safe_load(text)["azure"]["subscriptions"][0]["name"] == 'Prod: "main" #1'
    assert "timezone: America/Los_Angeles" in text


def test_local_timezone_from_env(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert init_mod.local_timezone() == "Asia/Tokyo"


def test_cli_init(root, monkeypatch, capsys):
    monkeypatch.setattr(init_mod, "run_az", fake_az)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False)
    assert cli.main(["init"]) == 2
    assert "pass --subscription" in capsys.readouterr().err
    monkeypatch.setattr("azure_finops.init.run_az", fake_az)
    assert cli.main(["init", "--subscription", "contoso-prod", "--timezone", "UTC"]) == 0
    out = capsys.readouterr().out
    assert "1 subscription(s), time zone UTC" in out and "finops doctor" in out
