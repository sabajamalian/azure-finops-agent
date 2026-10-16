"""Process-wide wiring: config, ARM client, evidence, datasets, audit."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from .arm import ArmClient
from .audit import AuditLog
from .config import Config, load_config, repo_root
from .datasets import DatasetStore
from .evidence import EvidenceLog
from .ratelimit import QpuLimiter


def fixture_mode() -> str | None:
    v = os.environ.get("FINOPS_FIXTURE_MODE", "").strip().lower()
    if v in ("", "0", "false", "off"):
        return None
    return "synthetic" if v in ("1", "true", "on") else v


@dataclass
class Runtime:
    config: Config
    arm: ArmClient
    evidence: EvidenceLog
    datasets: DatasetStore
    audit: AuditLog
    root: Path
    output_dir: Path | None = None
    fixed_today: date | None = None
    fixture: str | None = None
    capabilities: dict = field(default_factory=dict)

    def today(self) -> date:
        return self.fixed_today or datetime.now(UTC).date()


def build_runtime(
    config: Config | None = None,
    transport: httpx.BaseTransport | None = None,
    credential=None,
    output_dir: Path | None = None,
    fixed_today: date | None = None,
    cache_ttl: float | None = None,
    sleep=None,
) -> Runtime:
    root = repo_root()
    fx = fixture_mode()
    env_today = os.environ.get("FINOPS_FIXED_TODAY")
    if fixed_today is None and env_today:
        fixed_today = date.fromisoformat(env_today)
    if fx == "synthetic":
        from .simulator.synthetic import SyntheticAzure, synthetic_config

        # FINOPS_SYNTHETIC_DENY="cost:<sub>,arg:<sub>" simulates missing permissions (used by evals).
        deny = {
            tuple(x.strip().lower().split(":", 1))
            for x in os.environ.get("FINOPS_SYNTHETIC_DENY", "").split(",")
            if ":" in x
        }
        sim = SyntheticAzure(today=fixed_today, deny=deny)
        fixed_today = sim.today
        transport = transport or httpx.MockTransport(sim.handle)
        if config is None:
            config = load_config() if os.environ.get("FINOPS_CONFIG") else synthetic_config()
    if config is None:
        config = load_config()
    if transport is None and credential is None:
        from azure.identity import DefaultAzureCredential

        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)

    out_env = os.environ.get("FINOPS_OUTPUT_DIR")
    output_dir = output_dir or (Path(out_env).resolve() if out_env else None)
    audit = AuditLog(output_dir / "audit.jsonl" if output_dir else None)
    evidence = EvidenceLog(output_dir / "evidence.jsonl" if output_dir else None)
    if cache_ttl is None:
        cache_ttl = float(os.environ.get("FINOPS_CACHE_TTL", "0" if fx else "900"))
    cache_dir = Path(os.environ.get("FINOPS_CACHE_DIR", root / ".finops" / "cache"))
    kwargs: dict = {"sleep": sleep} if sleep else {}
    if fx:
        # Simulated responses have no throttling; don't make fixture runs wait on real QPU windows.
        kwargs["limiter"] = QpuLimiter(windows=((10.0, 10**9),))
    arm = ArmClient(
        config.subscription_ids(),
        credential=credential,
        transport=transport,
        audit=audit,
        cache_dir=cache_dir if cache_ttl > 0 else None,
        cache_ttl=cache_ttl,
        **kwargs,
    )
    return Runtime(config, arm, evidence, DatasetStore(), audit, root, output_dir, fixed_today, fx)


_RUNTIME: Runtime | None = None


def get_runtime() -> Runtime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = build_runtime()
    return _RUNTIME


def set_runtime(rt: Runtime | None) -> None:
    global _RUNTIME
    _RUNTIME = rt
