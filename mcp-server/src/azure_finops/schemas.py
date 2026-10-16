"""JSON Schemas for finops/config.yaml, automations, and result.json, generated from the pydantic models."""

from __future__ import annotations

import json
from pathlib import Path

from .automations import Automation
from .config import Config
from .results import Result

SCHEMAS = {
    "config.schema.json": (Config, "Azure FinOps agent configuration (finops/config.yaml)"),
    "automation.schema.json": (Automation, "Azure FinOps automation (finops/automations/<name>.yaml)"),
    "result.schema.json": (Result, "Result of a headless FinOps run (result.json, written by submit_result)"),
}


def render(name: str) -> str:
    model, title = SCHEMAS[name]
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": title,
        **{k: v for k, v in model.model_json_schema().items() if k != "title"},
    }
    return json.dumps(schema, indent=2, sort_keys=False) + "\n"


def export(root: Path, check: bool = False) -> list[str]:
    """Write schemas to finops/schemas/. With check=True, return the names that are out of date."""
    d = root / "finops" / "schemas"
    stale = []
    for name in SCHEMAS:
        text = render(name)
        p = d / name
        if check:
            if not p.exists() or p.read_text(encoding="utf-8") != text:
                stale.append(name)
        else:
            d.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
    return stale
