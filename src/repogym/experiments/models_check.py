"""Startup check: every model ID an experiment uses must be in the API's model list.

One ``models.list`` call per run start, no generation, so it costs no generation request. A
missing ID stops the run with a clear error before any worker starts. The matched entries are
saved as evidence (the run directory on every run; ``docs/week3/evidence/`` through
``repogym check-models``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

PREFIX = "models/"


class ModelCheckError(RuntimeError):
    """A configured model ID isn't served by the API (or the list couldn't be fetched)."""


def list_models(api_key: str, client=None) -> list[dict]:
    """The API's model list as plain dicts (name, display name, version, actions, limits)."""
    if client is None:
        from google import genai

        client = genai.Client(api_key=api_key)
    try:
        listed = list(client.models.list())
    except Exception as exc:
        raise ModelCheckError(f"could not fetch the model list: {type(exc).__name__}") from exc
    return [
        {
            "name": str(getattr(m, "name", "")),
            "display_name": getattr(m, "display_name", None),
            "version": getattr(m, "version", None),
            "supported_actions": list(getattr(m, "supported_actions", None) or []),
            "input_token_limit": getattr(m, "input_token_limit", None),
            "output_token_limit": getattr(m, "output_token_limit", None),
        }
        for m in listed
    ]


def verify(models: list[str], available: list[dict], when: datetime | None = None) -> dict:
    """Match configured IDs against the list. Returns the evidence record; raises if any is
    missing, naming the missing IDs and the closest listed ones."""
    by_id = {m["name"].removeprefix(PREFIX): m for m in available}
    missing = [m for m in models if m not in by_id]
    if missing:
        hints = {
            m: sorted(
                i for i in by_id if m.split("-")[0] in i or i.split("-")[0] == m.split("-")[0]
            )[:6]
            for m in missing
        }
        raise ModelCheckError(
            f"model ID(s) not in the API's model list: {missing}. "
            f"Listed models with the same family: {hints}. Fix configs/models.yaml."
        )
    return {
        "checked_at": (when or datetime.now(UTC)).isoformat(timespec="seconds"),
        "method": "models.list (no generation)",
        "models_listed": len(available),
        "configured": {m: by_id[m] for m in models},
    }


def check_models(api_key: str, models: list[str], out_dir: Path | None = None, client=None) -> dict:
    """List, verify, and save the evidence under ``out_dir`` if given."""
    record = verify(models, list_models(api_key, client))
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "models_list.json").write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record
