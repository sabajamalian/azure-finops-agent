"""Repository configuration (finops/config.yaml) and path helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GUID_RE = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
CostBasis = Literal["ActualCost", "AmortizedCost"]

# Cost Management grouping dimensions the tools accept. Tags use the "tag:<key>" form.
COST_DIMENSIONS = (
    "SubscriptionId",
    "ResourceGroupName",
    "ResourceId",
    "ResourceType",
    "ResourceLocation",
    "ServiceName",
    "ServiceFamily",
    "MeterCategory",
    "MeterSubCategory",
    "Meter",
    "ChargeType",
    "PricingModel",
    "PublisherType",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Subscription(_Strict):
    id: str = Field(pattern=GUID_RE, description="Azure subscription ID")
    name: str | None = Field(default=None, description="Friendly name used in reports")

    @field_validator("id")
    @classmethod
    def _lower(cls, v: str) -> str:
        return v.lower()


class AgentSettings(_Strict):
    name: str = Field(default="azure-finops", description="Agent name shown in outbound messages")
    model: str | None = Field(default=None, description="Model pinned for headless runs (copilot --model)")


class AzureSettings(_Strict):
    tenant_id: str = Field(pattern=GUID_RE)
    subscriptions: list[Subscription] = Field(min_length=1)
    cost_basis: CostBasis = Field(
        default="ActualCost", description="Default cost basis when a question doesn't specify one"
    )

    @model_validator(mode="after")
    def _unique(self) -> AzureSettings:
        ids = [s.id for s in self.subscriptions]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate subscription IDs in azure.subscriptions")
        return self


class AnomalySettings(_Strict):
    lookback_days: int = Field(default=60, ge=14, le=365)
    baseline_days: int = Field(default=28, ge=7, le=180)
    skip_recent_days: int = Field(
        default=2, ge=0, le=7, description="Most recent days excluded because cost data is still arriving"
    )
    min_impact: float = Field(
        default=50.0, ge=0, description="Minimum daily cost increase, in billing currency"
    )
    min_percent: float = Field(default=30.0, ge=0, description="Minimum percent increase over expected")
    z_threshold: float = Field(default=3.5, gt=0, description="Robust z-score threshold (median/MAD)")
    group_by: list[str] = Field(default_factory=lambda: ["ResourceGroupName", "ServiceName"], max_length=2)
    max_investigations_per_day: int = Field(default=3, ge=0, le=50)

    @field_validator("group_by")
    @classmethod
    def _dims(cls, v: list[str]) -> list[str]:
        for d in v:
            if d not in COST_DIMENSIONS and not d.startswith("tag:"):
                raise ValueError(f"unsupported anomaly group_by dimension: {d}")
        return v


class Destination(_Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    type: Literal["github_issue"] = "github_issue"
    labels: list[str] = Field(default_factory=list, description="Labels applied to issues created here")


class Config(_Strict):
    version: Literal[1] = 1
    agent: AgentSettings = Field(default_factory=AgentSettings)
    azure: AzureSettings
    timezone: str = Field(default="UTC", description="IANA timezone used for reports and schedules")
    anomaly: AnomalySettings = Field(default_factory=AnomalySettings)
    destinations: list[Destination] = Field(min_length=1)
    retention_days: int = Field(
        default=30, ge=1, le=400, description="Actions artifact retention for run records"
    )
    max_issues_per_run: int = Field(default=5, ge=0, le=25)
    allow_public_repo_output: bool = Field(
        default=False, description="Allow posting spend data when the repository is public. Keep false."
    )

    @model_validator(mode="after")
    def _check(self) -> Config:
        ids = [d.id for d in self.destinations]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate destination ids")
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(self.timezone)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"unknown timezone {self.timezone!r}") from exc
        return self

    def subscription_ids(self) -> list[str]:
        return [s.id for s in self.azure.subscriptions]

    def subscription_name(self, sub_id: str) -> str:
        for s in self.azure.subscriptions:
            if s.id == sub_id.lower():
                return s.name or s.id
        return sub_id

    def destination(self, dest_id: str) -> Destination | None:
        return next((d for d in self.destinations if d.id == dest_id), None)


def repo_root(start: Path | None = None) -> Path:
    env = os.environ.get("FINOPS_REPO_ROOT")
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for p in [here, *here.parents]:
        if (p / "finops").is_dir() and ((p / ".git").exists() or (p / ".github").is_dir()):
            return p
    return here


def config_path(root: Path | None = None) -> Path:
    env = os.environ.get("FINOPS_CONFIG")
    if env:
        return Path(env).resolve()
    return (root or repo_root()) / "finops" / "config.yaml"


class ConfigError(Exception):
    pass


def load_config(path: Path | None = None) -> Config:
    p = path or config_path()
    if not p.exists():
        raise ConfigError(
            f"{p} not found. Copy finops/config.example.yaml to finops/config.yaml and fill in your tenant "
            "and subscriptions, or set FINOPS_CONFIG."
        )
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    try:
        return Config.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"{p}: {exc}") from exc


_SUB_IN_SCOPE = re.compile(
    r"/subscriptions/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"(?:/resourceGroups/([^/]+))?/?",
    re.IGNORECASE,
)
# Azure resource group names: 1-90 letters, digits, '_', '-', '.', '(' or ')'; can't end with '.'.
_RG_NAME = re.compile(r"[-\w.()]{0,89}[-\w()]")


def parse_scope(scope: str) -> tuple[str, str | None]:
    """Return (subscription_id, resource_group|None) for a subscription or resource-group scope.

    Accepts a bare subscription GUID as shorthand.
    """
    s = scope.strip()
    if re.match(GUID_RE, s):
        return s.lower(), None
    m = _SUB_IN_SCOPE.fullmatch(s)
    if not m:
        raise ValueError(
            f"unsupported scope {scope!r}; use a subscription ID, /subscriptions/<id>, "
            "or /subscriptions/<id>/resourceGroups/<name>"
        )
    rg = m.group(2)
    if rg is not None and not _RG_NAME.fullmatch(rg):
        raise ValueError(f"invalid resource group name {rg!r}")
    return m.group(1).lower(), rg
