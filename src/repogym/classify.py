"""LLM task-type classifier for tasks whose labels are inconclusive.

Labels are written to data/task_type_labels.jsonl and are *not* applied to verified tasks; that
happens only after calibration against human labels.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Literal

import structlog
from pydantic import BaseModel

from repogym.llm import GeminiClient, LLMBadResponse, LLMRequestError, LLMUnavailable
from repogym.models import Candidate, TaskType
from repogym.tasktype import TaskTypeClassifier

log = structlog.get_logger()

PROMPT_VERSION = "task-type-v1"
ISSUE_BODY_LIMIT = 3000
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANK_RUNS = re.compile(r"\n{3,}")

LlmType = Literal["bug", "feature", "other"]
GENERATION_CONFIG = {
    "temperature": 0,
    "responseMimeType": "application/json",
    "responseSchema": {
        "type": "OBJECT",
        "properties": {
            "task_type": {"type": "STRING", "enum": ["bug", "feature", "other"]},
            "confidence": {"type": "NUMBER"},
            "rationale": {"type": "STRING"},
        },
        "required": ["task_type", "confidence", "rationale"],
    },
}

PROMPT = """\
You are labelling GitHub pull requests for a dataset of coding tasks.

Decide what the pull request does, judging from the issue it closes:
- "bug": fixes incorrect behaviour, a crash, an error, a regression, or a mismatch with
  documented or clearly intended behaviour.
- "feature": adds new behaviour, a new option, parameter, API or capability, or extends
  support to something that was never supported.
- "other": neither, for example documentation, refactoring, performance, typing-only or
  compatibility/maintenance work.

Respond with JSON only: {{"task_type": "bug"|"feature"|"other", "confidence": <number 0-1>,
"rationale": "<one or two sentences>"}}

Repository: {repo}
Issue title: {issue_title}
Issue body:
{issue_body}

Pull request title: {pr_title}
Pull request labels: {pr_labels}
"""


class MalformedOutput(ValueError):
    pass


class LlmLabel(BaseModel):
    task_type: LlmType
    confidence: float
    rationale: str


class TaskTypeLabel(BaseModel):
    task_id: str
    task_type: Literal["bug", "feature", "other", "unknown"]
    confidence: float | None = None
    rationale: str = ""
    model: str | None = None
    prompt_version: str = PROMPT_VERSION
    finish_reason: str | None = None
    cached: bool = False
    run: int | None = None  # consensus run index; None for single-run labels
    error: str = ""
    labelled_at: str


def truncate_body(body: str, limit: int = ISSUE_BODY_LIMIT) -> str:
    """Drop template comments and blank runs, then cut at a line break near the limit."""
    text = _BLANK_RUNS.sub("\n\n", _COMMENT.sub("", body or "")).strip()
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit)
    if cut < limit // 2:
        cut = limit
    return text[:cut].rstrip() + f"\n[... truncated, {len(text) - cut} more characters]"


def build_prompt(candidate: Candidate) -> str:
    return PROMPT.format(
        repo=candidate.repo,
        issue_title=candidate.issue_title,
        issue_body=truncate_body(candidate.issue_body) or "(empty)",
        pr_title=candidate.pr_title or "(unknown)",
        pr_labels=", ".join(candidate.pr_labels) or "(none)",
    )


def parse_label(text: str) -> LlmLabel:
    """Strictly validate the model's JSON; anything else is MalformedOutput."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MalformedOutput(f"not JSON: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise MalformedOutput("not a JSON object")
    extra = set(data) - {"task_type", "confidence", "rationale"}
    if extra:
        raise MalformedOutput(f"unexpected keys: {sorted(extra)}")
    try:
        label = LlmLabel.model_validate(data, strict=False)
    except ValueError as exc:
        raise MalformedOutput(f"schema: {exc.errors()[0]['msg']}") from exc  # type: ignore[attr-defined]
    if isinstance(data.get("confidence"), bool) or not 0 <= label.confidence <= 1:
        raise MalformedOutput("confidence must be a number in [0, 1]")
    if not label.rationale.strip():
        raise MalformedOutput("empty rationale")
    return label


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cache_key(candidate: Candidate, run: int | None = None) -> str:
    """Prompt version + prompt; consensus runs add their index so each is a separate request.

    ``run=None`` keeps the key used by the single-run calibration labels.
    """
    prefix = PROMPT_VERSION if run is None else f"{PROMPT_VERSION}\nrun={run}"
    return f"{prefix}\n{build_prompt(candidate)}"


def classify(
    candidate: Candidate, client: GeminiClient, refresh: bool = False, run: int | None = None
) -> TaskTypeLabel:
    """Label one task, or mark it "unknown" with the error. Never guesses a label.

    Malformed JSON and incomplete answers (no candidates, empty text, finishReason other than
    STOP) get one fresh, uncached retry. Nothing that fails validation is ever cached.
    """
    prompt = build_prompt(candidate)
    key = cache_key(candidate, run)

    def unknown(error: str) -> TaskTypeLabel:
        log.warning("classify_failed", task=candidate.task_id, error=error)
        return TaskTypeLabel(
            task_id=candidate.task_id, task_type="unknown", error=error, run=run,
            labelled_at=_now(),
        )  # fmt: skip

    error = ""
    for attempt in range(2):
        try:
            completion = client.complete(
                prompt,
                key,
                GENERATION_CONFIG,
                use_cache=attempt == 0 and not refresh,
                validate=parse_label,
            )
            label = parse_label(completion.text)  # cache hits are re-checked too
        except (MalformedOutput, LLMBadResponse) as exc:
            # A bad cache entry (if that's where it came from) must not be replayed.
            for model in {client.settings.model, client.settings.fallback_model}:
                hit = client.cached(model, key)
                if hit is not None and _invalid(hit.text):
                    client.forget(model, key)
            error = f"{type(exc).__name__}: {exc}"
            log.warning("classify_bad_output", task=candidate.task_id, attempt=attempt + 1,
                        error=error)  # fmt: skip
            continue
        except (LLMUnavailable, LLMRequestError) as exc:
            return unknown(str(exc))
        return TaskTypeLabel(
            task_id=candidate.task_id,
            task_type=label.task_type,
            confidence=label.confidence,
            rationale=label.rationale,
            model=completion.model,
            finish_reason=completion.finish_reason,
            cached=completion.cached,
            run=run,
            labelled_at=_now(),
        )
    return unknown(f"bad output twice; last: {error}")


def _invalid(text: str) -> bool:
    try:
        parse_label(text)
    except MalformedOutput:
        return True
    return False


def make_classifier(client: GeminiClient) -> TaskTypeClassifier:
    """Adapter for repogym.tasktype's hook. "other" is not a task_type yet, so it stays unknown."""

    def classifier(candidate: Candidate) -> TaskType | None:
        label = classify(candidate, client)
        return label.task_type if label.task_type in ("bug", "feature") else None

    return classifier
