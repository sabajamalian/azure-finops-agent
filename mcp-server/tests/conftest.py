from __future__ import annotations

import httpx
import pytest

from azure_finops.runtime import Runtime, build_runtime, set_runtime
from azure_finops.simulator.synthetic import DEFAULT_TODAY, DEV, PROD, SyntheticAzure, synthetic_config

PROD_SCOPE = f"/subscriptions/{PROD}"
DEV_SCOPE = f"/subscriptions/{DEV}"


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A throwaway repository root with the finops/ layout."""
    for d in ("finops/context", "finops/automations", "finops/schemas", ".github"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("FINOPS_REPO_ROOT", str(tmp_path))
    for var in (
        "FINOPS_FIXTURE_MODE",
        "FINOPS_CONFIG",
        "FINOPS_OUTPUT_DIR",
        "FINOPS_FIXED_TODAY",
        "FINOPS_CACHE_TTL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("FINOPS_CACHE_DIR", str(tmp_path / ".finops" / "cache"))
    return tmp_path


@pytest.fixture
def sim() -> SyntheticAzure:
    return SyntheticAzure(today=DEFAULT_TODAY)


def make_runtime(sim: SyntheticAzure, output_dir=None, config=None, cache_ttl: float = 0) -> Runtime:
    return build_runtime(
        config=config or synthetic_config(),
        transport=httpx.MockTransport(sim.handle),
        credential=None,
        output_dir=output_dir,
        fixed_today=sim.today,
        cache_ttl=cache_ttl,
        sleep=lambda _s: None,
    )


@pytest.fixture
def rt(repo, sim):
    """Runtime wired to the synthetic Azure simulator; also installed as the global runtime for server tools."""
    runtime = make_runtime(sim)
    set_runtime(runtime)
    yield runtime
    set_runtime(None)


@pytest.fixture
def rt_out(repo, sim, tmp_path):
    """Like rt, but with an output dir (headless mode)."""
    out = tmp_path / "out"
    out.mkdir()
    runtime = make_runtime(sim, output_dir=out)
    set_runtime(runtime)
    yield runtime
    set_runtime(None)
