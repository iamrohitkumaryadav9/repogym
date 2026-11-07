"""Classifier, Gemini client retry/fallback, and calibration metrics. No network: a fake transport."""

import json
import random

import httpx
import pytest

from repogym.calibrate import agreement, cohen_kappa, precision_recall
from repogym.classify import (
    MalformedOutput,
    build_prompt,
    classify,
    make_classifier,
    parse_label,
    truncate_body,
)
from repogym.llm import (
    GeminiClient,
    LLMBadResponse,
    LLMRequestError,
    LLMUnavailable,
    MissingSettings,
    Settings,
    Throttle,
    backoff_delays,
)
from tests.test_validate import _candidate

SETTINGS = Settings(api_key="test-key-not-real", model="primary-m", fallback_model="fallback-m")
GOOD = json.dumps({"task_type": "bug", "confidence": 0.9, "rationale": "Fixes a crash."})


def gemini_response(text, status=200, finish_reason="STOP"):
    candidate = {"content": {"parts": [{"text": text}]}, "finishReason": finish_reason}
    body = {"candidates": [candidate]} if status == 200 else {"error": {"code": status}}
    return httpx.Response(status, json=body)


class FakeTransport:
    """Replays scripted responses per model and records every call."""

    def __init__(self, script):
        self.script = {model: list(items) for model, items in script.items()}
        self.calls = []

    def __call__(self, model, prompt, config):
        self.calls.append(model)
        item = self.script[model].pop(0)
        if isinstance(item, Exception):
            raise item
        return item if isinstance(item, httpx.Response) else gemini_response(item)


def client(tmp_path, script, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return GeminiClient(
        SETTINGS,
        interval=0,
        cache_dir=tmp_path,
        transport=FakeTransport(script),
        sleep=sleeps.append,
        rng=random.Random(0),
    )


# -- settings --------------------------------------------------------------------------------


def test_missing_settings_are_named_without_values(tmp_path, monkeypatch):
    monkeypatch.setattr("repogym.llm.ROOT", tmp_path)  # no .env there
    for name in ("GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_FALLBACK_MODEL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "m")
    with pytest.raises(MissingSettings, match="GEMINI_API_KEY, GEMINI_FALLBACK_MODEL"):
        Settings.from_env()


def test_api_key_is_not_in_repr():
    assert "test-key-not-real" not in repr(SETTINGS)


# -- retries, fallback, cache ----------------------------------------------------------------


def test_backoff_schedule():
    assert backoff_delays() == [2, 4, 8, 16]


def test_retries_on_503_429_500_with_jittered_backoff(tmp_path):
    sleeps = []
    c = client(tmp_path, {"primary-m": [gemini_response("", 503), gemini_response("", 429),
                                        gemini_response("", 500), GOOD]}, sleeps)  # fmt: skip
    result = c.complete("p", "k", {})
    assert (result.text, result.model, result.cached) == (GOOD, "primary-m", False)
    assert len(sleeps) == 3
    for slept, nominal in zip(sleeps, [2, 4, 8], strict=True):
        assert 0.5 * nominal <= slept <= 1.5 * nominal


def test_transport_errors_are_retried(tmp_path):
    c = client(tmp_path, {"primary-m": [httpx.ConnectError("boom"), GOOD]})
    assert c.complete("p", "k", {}).text == GOOD


def test_falls_back_only_after_five_failed_attempts(tmp_path):
    sleeps = []
    c = client(
        tmp_path,
        {"primary-m": [gemini_response("", 503)] * 5, "fallback-m": [GOOD]},
        sleeps,
    )
    result = c.complete("p", "k", {})
    assert result.model == "fallback-m"
    assert c._transport.calls == ["primary-m"] * 5 + ["fallback-m"]
    assert len(sleeps) == 4  # between the five primary attempts


def test_fallback_uses_the_same_retry_policy_then_gives_up(tmp_path):
    c = client(
        tmp_path,
        {"primary-m": [gemini_response("", 503)] * 5, "fallback-m": [gemini_response("", 429)] * 5},
    )
    with pytest.raises(LLMUnavailable):
        c.complete("p", "k", {})
    assert c._transport.calls.count("fallback-m") == 5


def test_non_retryable_errors_fail_fast_without_fallback(tmp_path):
    c = client(tmp_path, {"primary-m": [gemini_response("", 403)]})
    with pytest.raises(LLMRequestError, match="HTTP 403"):
        c.complete("p", "k", {})
    assert c._transport.calls == ["primary-m"]


def test_successful_responses_are_cached(tmp_path):
    c = client(tmp_path, {"primary-m": [GOOD]})
    c.complete("p", "k", {})
    again = c.complete("p", "k", {})
    assert again.cached and c._transport.calls == ["primary-m"]
    assert "test-key-not-real" not in "".join(p.read_text() for p in tmp_path.rglob("*.json"))


def test_rerun_retries_primary_before_reusing_a_fallback_answer(tmp_path):
    c = client(tmp_path, {"primary-m": [gemini_response("", 503)] * 5, "fallback-m": [GOOD]})
    assert c.complete("p", "k", {}).model == "fallback-m"
    primary_ok = json.dumps({"task_type": "feature", "confidence": 0.7, "rationale": "New."})
    c._transport.script["primary-m"] = [primary_ok]
    result = c.complete("p", "k", {})
    assert result.model == "primary-m" and result.text == primary_ok


def test_throttle_spaces_requests():
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    throttle = Throttle(5.0, clock=lambda: now[0], sleep=sleep)
    throttle.wait()
    now[0] += 1.0
    throttle.wait()
    throttle.wait()
    assert sleeps == [4.0, 5.0]


# -- JSON parsing ----------------------------------------------------------------------------


def test_parse_valid_label():
    label = parse_label(GOOD)
    assert (label.task_type, label.confidence) == ("bug", 0.9)


@pytest.mark.parametrize(
    "text",
    [
        "not json",
        "[1, 2]",
        '```json\n{"task_type": "bug", "confidence": 0.9, "rationale": "x"}\n```',
        '{"task_type": "chore", "confidence": 0.9, "rationale": "x"}',
        '{"task_type": "bug", "confidence": 1.5, "rationale": "x"}',
        '{"task_type": "bug", "confidence": true, "rationale": "x"}',
        '{"task_type": "bug", "confidence": "high", "rationale": "x"}',
        '{"task_type": "bug", "confidence": 0.9, "rationale": "  "}',
        '{"task_type": "bug", "confidence": 0.9}',
        '{"task_type": "bug", "confidence": 0.9, "rationale": "x", "extra": 1}',
    ],
)
def test_parse_rejects_malformed_output(text):
    with pytest.raises(MalformedOutput):
        parse_label(text)


def test_malformed_output_is_retried_once_uncached(tmp_path):
    c = client(tmp_path, {"primary-m": ["garbage", GOOD]})
    label = classify(_candidate(), c)
    assert label.task_type == "bug" and label.model == "primary-m" and label.error == ""
    assert c._transport.calls == ["primary-m", "primary-m"]


def test_malformed_twice_marks_unknown_and_caches_nothing(tmp_path):
    c = client(tmp_path, {"primary-m": ["garbage", "{}"]})
    label = classify(_candidate(), c)
    assert label.task_type == "unknown" and "MalformedOutput" in label.error
    assert not list(tmp_path.rglob("*.json"))


def test_unavailable_api_marks_unknown(tmp_path):
    c = client(
        tmp_path,
        {"primary-m": [gemini_response("", 503)] * 5, "fallback-m": [gemini_response("", 503)] * 5},
    )
    label = classify(_candidate(), c)
    assert label.task_type == "unknown" and label.model is None and label.error


def test_label_records_model_and_prompt_version(tmp_path):
    c = client(tmp_path, {"primary-m": [gemini_response("", 503)] * 5, "fallback-m": [GOOD]})
    label = classify(_candidate(), c)
    assert label.model == "fallback-m" and label.prompt_version == "task-type-v1"


def test_hook_maps_other_to_unknown(tmp_path):
    other = json.dumps({"task_type": "other", "confidence": 0.8, "rationale": "Docs only."})
    c = client(tmp_path, {"primary-m": [other]})
    assert make_classifier(c)(_candidate()) is None


def test_prompt_contains_inputs_and_truncates_long_bodies():
    candidate = _candidate().model_copy(
        update={
            "issue_body": "<!-- template -->" + "line\n" * 2000,
            "pr_title": "Fix it",
            "pr_labels": ["bug"],
        }  # fmt: skip
    )
    prompt = build_prompt(candidate)
    assert "Fix it" in prompt and "bug" in prompt and "template" not in prompt
    assert "[... truncated" in prompt and len(prompt) < 5000


def test_truncate_keeps_short_bodies():
    assert truncate_body("short\n\n\n\nbody") == "short\n\nbody"


# -- incomplete answers and cache hygiene -----------------------------------------------------


def cache_files(path):
    return list(path.rglob("*.json"))


@pytest.mark.parametrize("status", [429, 500, 503, 400, 403])
def test_error_responses_are_never_cached(tmp_path, status):
    c = client(tmp_path, {"primary-m": [gemini_response("", status)] * 5,
                          "fallback-m": [gemini_response("", status)] * 5})  # fmt: skip
    with pytest.raises((LLMUnavailable, LLMRequestError)):
        c.complete("p", "k", {})
    assert cache_files(tmp_path) == []


@pytest.mark.parametrize("reason", ["SAFETY", "MAX_TOKENS", "RECITATION", "OTHER", None])
def test_non_stop_finish_reason_is_a_failure_even_with_valid_text(tmp_path, reason):
    c = client(tmp_path, {"primary-m": [gemini_response(GOOD, finish_reason=reason)]})
    with pytest.raises(LLMBadResponse, match="finishReason"):
        c.complete("p", "k", {})
    assert cache_files(tmp_path) == []


@pytest.mark.parametrize(
    "body",
    [
        {"candidates": []},
        {"promptFeedback": {"blockReason": "SAFETY"}},
        {"candidates": [{"content": {"parts": [{"text": "  "}]}, "finishReason": "STOP"}]},
        {"candidates": [{"finishReason": "STOP"}]},
    ],
)
def test_missing_candidates_or_empty_text_is_a_failure(tmp_path, body):
    c = client(tmp_path, {"primary-m": [httpx.Response(200, json=body)]})
    with pytest.raises(LLMBadResponse):
        c.complete("p", "k", {})


def test_truncated_answer_marks_unknown_never_a_guess(tmp_path):
    partial = '{"task_type": "bug", "confidence": 0.9, "rati'
    c = client(tmp_path, {"primary-m": [gemini_response(partial, finish_reason="MAX_TOKENS"),
                                        gemini_response(partial, finish_reason="MAX_TOKENS")]})  # fmt: skip
    label = classify(_candidate(), c)
    assert label.task_type == "unknown" and label.confidence is None and label.model is None
    assert "MAX_TOKENS" in label.error
    assert cache_files(tmp_path) == []


def test_safety_block_then_good_answer_is_used(tmp_path):
    c = client(tmp_path, {"primary-m": [gemini_response("", finish_reason="SAFETY"), GOOD]})
    label = classify(_candidate(), c)
    assert label.task_type == "bug" and label.finish_reason == "STOP"


def test_schema_invalid_answer_is_not_cached_even_briefly(tmp_path):
    seen = []

    def check(text):
        seen.append(cache_files(tmp_path))
        return parse_label(text)

    c = client(tmp_path, {"primary-m": ["garbage"]})
    with pytest.raises(MalformedOutput):
        c.complete("p", "k", {}, validate=check)
    assert seen == [[]] and cache_files(tmp_path) == []


def test_exhausted_retries_on_every_model_mark_unknown(tmp_path):
    c = client(tmp_path, {"primary-m": [gemini_response("", 429)] * 5,
                          "fallback-m": [gemini_response("", 503)] * 5})  # fmt: skip
    label = classify(_candidate(), c)
    assert label.task_type == "unknown" and label.confidence is None
    assert "5 attempts failed" in label.error
    assert c._transport.calls == ["primary-m"] * 5 + ["fallback-m"] * 5


def test_same_primary_and_fallback_is_not_retried_twice(tmp_path):
    same = Settings(api_key="test-key-not-real", model="only-m", fallback_model="only-m")
    transport = FakeTransport({"only-m": [gemini_response("", 503)] * 5})
    c = GeminiClient(same, interval=0, cache_dir=tmp_path, transport=transport,
                     sleep=lambda s: None, rng=random.Random(0))  # fmt: skip
    with pytest.raises(LLMUnavailable):
        c.complete("p", "k", {})
    assert transport.calls == ["only-m"] * 5


def test_cache_records_finish_reason_and_audit_flags_bad_entries(tmp_path):
    c = client(tmp_path, {"primary-m": [GOOD]})
    c.complete("p", "good", {}, validate=parse_label)
    c.store("primary-m", "bad", "not json", "STOP")
    c.store("primary-m", "blocked", GOOD, "SAFETY")
    assert c.cached("primary-m", "good").finish_reason == "STOP"
    total, problems = c.audit_cache(parse_label)
    assert total == 3
    assert sorted(p.problem.split(":")[0] for p in problems) == ["MalformedOutput", "ValueError"]


def test_bad_cache_entry_is_dropped_and_refetched(tmp_path):
    c = client(tmp_path, {"primary-m": [GOOD]})
    c.store("primary-m", __import__("repogym.classify").classify.cache_key(_candidate()),
            "garbage", "STOP")  # fmt: skip
    label = classify(_candidate(), c)
    assert label.task_type == "bug" and not label.cached
    assert c._transport.calls == ["primary-m"]


def test_requests_are_deterministic_temperature_zero(tmp_path):
    from repogym.classify import GENERATION_CONFIG

    seen = []
    transport = FakeTransport({"primary-m": [GOOD]})

    def recording(model, prompt, config):
        seen.append(config)
        return transport(model, prompt, config)

    c = GeminiClient(SETTINGS, interval=0, cache_dir=tmp_path, transport=recording,
                     sleep=lambda s: None)  # fmt: skip
    classify(_candidate(), c)
    assert seen == [GENERATION_CONFIG] and GENERATION_CONFIG["temperature"] == 0


def test_refresh_bypasses_the_cache(tmp_path):
    c = client(tmp_path, {"primary-m": [GOOD, GOOD]})
    classify(_candidate(), c)
    label = classify(_candidate(), c, refresh=True)
    assert not label.cached and c._transport.calls == ["primary-m", "primary-m"]


# -- metrics ---------------------------------------------------------------------------------


def test_cohen_kappa_known_values():
    assert cohen_kappa(["a", "b", "a", "b"], ["a", "b", "a", "b"]) == 1.0
    # observed 0.5, expected 0.5 -> 0
    assert cohen_kappa(["a", "a", "b", "b"], ["a", "b", "a", "b"]) == 0.0
    # textbook example: 20 yes/yes, 5 yes/no, 10 no/yes, 15 no/no -> 0.4
    a = ["y"] * 25 + ["n"] * 25
    b = ["y"] * 20 + ["n"] * 5 + ["y"] * 10 + ["n"] * 15
    assert cohen_kappa(a, b) == pytest.approx(0.4)


def test_cohen_kappa_undefined_for_a_single_shared_label():
    assert cohen_kappa(["a", "a"], ["a", "a"]) is None


def test_agreement_reports_accuracy_and_confusion():
    result = agreement(["bug", "bug", "feature"], ["bug", "feature", "feature"])
    assert result.n == 3 and result.accuracy == pytest.approx(2 / 3)
    assert result.confusion[("bug", "feature")] == 1


def test_precision_recall():
    pr = precision_recall(actual=[True, True, False, False], flagged=[True, False, True, False])
    assert (pr.precision, pr.recall) == (0.5, 0.5)
    assert precision_recall([False], [False]).precision is None
