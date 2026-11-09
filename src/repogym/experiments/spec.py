"""Experiment files: what to run, with every parameter pinned and hashed.

An experiment is a grid of cells, model x strategy x task x repeat. Each cell is one episode,
identified by a stable key, so runs are resumable and idempotent. The provenance recorded in
every episode includes the experiment file's hash and a hash of the fully resolved config.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from repogym.config import RELEASE_DIR, ROOT
from repogym.context.base import ContextConfig
from repogym.context.strategies import STRATEGIES
from repogym.context.tokens import SAFETY, TokenEstimator
from repogym.env.budgets import Budgets
from repogym.providers.retry import RetryPolicy
from repogym.release import ReleaseTask
from repogym.tools.definitions import TOOLS

KINDS = ("pilot", "e1", "e2", "e3", "adhoc")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(obj) -> str:
    return sha256_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode())


@dataclass(frozen=True)
class Cell:
    model: str
    strategy: str
    task_id: str
    repeat: int

    @property
    def key(self) -> str:
        return f"{self.model}__{self.strategy}__{self.task_id}__r{self.repeat}"


@dataclass
class Experiment:
    name: str
    kind: str
    split: str
    task_ids: list[str]
    models: list[str]
    strategies: list[str]
    repeats: int
    context: ContextConfig
    budgets: Budgets
    model_settings: dict[str, dict]
    limits_path: Path
    release: str = "v0.1"
    repeat_start: int = 0
    frozen_config: dict | None = None  # {"path": ..., "sha256": ...}; required for test runs
    path: Path | None = None
    file_sha256: str = ""
    runs_root: Path | None = None  # default data/runs (tests override it)
    # Pre-registered retry thresholds (D16): 5xx in-place backoff, daily-quota hold.
    retry: RetryPolicy = field(default_factory=lambda: RetryPolicy(pause_on_daily=True))
    raw: dict = field(default_factory=dict)

    @property
    def run_dir(self) -> Path:
        return (self.runs_root or ROOT / "data" / "runs") / self.name

    def cells(self) -> list[Cell]:
        return [
            Cell(m, s, t, r)
            for m in self.models
            for s in self.strategies
            for t in self.task_ids
            for r in range(self.repeat_start, self.repeat_start + self.repeats)
        ]

    def resolved(self) -> dict:
        """Everything that determines behaviour, for hashing and provenance."""
        return {
            "kind": self.kind,
            "split": self.split,
            "task_ids": self.task_ids,
            "models": self.models,
            "strategies": self.strategies,
            "repeats": self.repeats,
            "repeat_start": self.repeat_start,
            "context": self.context.as_dict(),
            "budgets": self.budgets.model_dump(),
            "retry": self.retry.as_dict(),
            "release": self.release,
            "model_settings": {m: self.model_settings[m] for m in self.models},
            "limits_sha256": sha256_bytes(self.limits_path.read_bytes()),
            "limits_margin": yaml.safe_load(self.limits_path.read_text())["margin"],
            "prompts": agent_prompts(),
            "tools": TOOLS,
        }

    @property
    def resolved_sha256(self) -> str:
        return sha256_json(self.resolved())


def agent_prompts() -> dict:
    """The agent's fixed prompt texts (frozen before E3 with everything else)."""
    import inspect

    from repogym.agents import tool_agent
    from repogym.context import base
    from repogym.env import environment

    return {
        "system": tool_agent.SYSTEM_PROMPT,
        "view_text": tool_agent.VIEW_TEXT,
        "strategy_view": {k: list(v) for k, v in tool_agent.STRATEGY_VIEW.items()},
        "step_counter": tool_agent.STEP_COUNTER,
        "final_nudge": [tool_agent.FINAL_NUDGE, tool_agent.FINAL_STEPS],
        "reasoning_reminder": [tool_agent.REASONING_REMINDER, tool_agent.REMIND_EVERY],
        "env_instructions": inspect.getsource(environment.instructions),
        "summary_system": tool_agent.SUMMARY_SYSTEM,
        "no_tool_nudge": tool_agent.NO_TOOL_NUDGE,
        "summary_header": base.SUMMARY_HEADER,
        "mask_text": base.MASK_TEXT,
        "clip_marker": base.CLIP_MARKER,
    }


def release_tasks(release: str = "v0.1") -> dict[str, ReleaseTask]:
    path = RELEASE_DIR / release / "tasks.jsonl"
    tasks = [ReleaseTask.model_validate_json(line) for line in path.read_text().splitlines()]
    return {t.task_id: t for t in tasks}


class ExperimentError(ValueError):
    pass


def max_clip_chars(context: ContextConfig) -> int:
    """The most characters the prompt clip can ever keep (estimator at its lowest factor)."""
    est = TokenEstimator()
    return int(context.clip_tokens * est.chars_per_token / (est.bounds[0] * SAFETY))


def check_observation_caps(context: ContextConfig, budgets: Budgets) -> None:
    """D14: the stored-observation cap must exceed anything the clip could show the model.

    Otherwise the environment would truncate text the model was meant to see, and the clip
    would silently become a no-op on top of a harsher, untracked cut.
    """
    limit = max_clip_chars(context)
    if budgets.max_observation_chars <= limit:
        raise ExperimentError(
            f"budgets.max_observation_chars={budgets.max_observation_chars} must exceed the "
            f"largest prompt clip ({limit} chars for clip_tokens={context.clip_tokens}); see D14"
        )


def load(path: Path) -> Experiment:
    raw_bytes = path.read_bytes()
    data = yaml.safe_load(raw_bytes)
    kind = data.get("kind")
    if kind not in KINDS:
        raise ExperimentError(f"kind must be one of {KINDS}")
    tasks = release_tasks(data.get("release", "v0.1"))
    split = data["split"]
    task_ids = data.get("tasks", "all")
    if task_ids == "all":
        task_ids = sorted(t for t, task in tasks.items() if task.split == split)
    unknown = [t for t in task_ids if t not in tasks]
    if unknown:
        raise ExperimentError(f"unknown task ids: {unknown[:5]}")
    wrong_split = [t for t in task_ids if tasks[t].split != split]
    if wrong_split:
        raise ExperimentError(f"tasks not in split {split!r}: {wrong_split[:5]}")
    strategies = data["strategies"]
    bad = [s for s in strategies if s not in STRATEGIES]
    if bad:
        raise ExperimentError(f"unknown strategies {bad}; choose from {STRATEGIES}")
    models_file = ROOT / data.get("models_config", "configs/models.yaml")
    all_settings = yaml.safe_load(models_file.read_text())["models"]
    missing = [m for m in data["models"] if m not in all_settings]
    if missing:
        raise ExperimentError(f"models not in {models_file.name}: {missing}")
    # Per-experiment overrides of pinned model settings (e.g. a thinking-level check, D22).
    # They are part of model_settings, so they are hashed and recorded in every episode.
    overrides = data.get("model_overrides") or {}
    bad = sorted(set(overrides) - set(data["models"]))
    if bad:
        raise ExperimentError(f"model_overrides for models not in the experiment: {bad}")
    all_settings = {m: {**v, **overrides.get(m, {})} for m, v in all_settings.items()}
    counting = [
        m for m in data["models"] if all_settings[m].get("token_counting", "estimate") != "estimate"
    ]
    if counting:  # D20: countTokens is for offline validation only, never in a timed run
        raise ExperimentError(f"token_counting other than 'estimate' for {counting} (D20)")
    context = ContextConfig(**data.get("context", {}))
    budgets = Budgets(**data.get("budgets", {}))
    check_observation_caps(context, budgets)
    return Experiment(
        name=data["name"],
        kind=kind,
        split=split,
        task_ids=list(task_ids),
        models=list(data["models"]),
        strategies=list(strategies),
        repeats=int(data.get("repeats", 1)),
        repeat_start=int(data.get("repeat_start", 0)),
        context=context,
        budgets=budgets,
        model_settings={m: all_settings[m] for m in data["models"]},
        limits_path=ROOT / data.get("limits", "configs/limits/gemini_free_tier.yaml"),
        release=data.get("release", "v0.1"),
        frozen_config=data.get("frozen_config"),
        retry=RetryPolicy(**{"pause_on_daily": True, **(data.get("retry") or {})}),
        path=path,
        file_sha256=sha256_bytes(raw_bytes),
        raw=data,
    )
