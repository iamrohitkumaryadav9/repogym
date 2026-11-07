"""Baseline agent and the provider layer, fully mocked (no network)."""

import random

import httpx
import pytest

from repogym.agents.baseline import SENTINEL, BaselineAgent, parse_command
from repogym.agents.models import (
    GeminiChat,
    ModelResponseError,
    ModelUnavailable,
    OpenAICompatChat,
    Reply,
)
from repogym.env.budgets import Budgets
from repogym.env.environment import Observation
from repogym.llm import GeminiClient, Settings
from tests.test_episode import env_with

SETTINGS = Settings(api_key="test-key-not-real", model="primary-m", fallback_model="fallback-m")


def test_parse_command_needs_exactly_one_block():
    assert parse_command("THOUGHT: x\n```bash\nls -la\n```") == ("ls -la", None)
    assert parse_command("```sh\npwd\n```")[0] == "pwd"
    assert "exactly one" in parse_command("no block")[1]
    assert "had 2" in parse_command("```bash\na\n```\n```bash\nb\n```")[1]
    assert "empty" in parse_command("```bash\n\n```")[1]


class ScriptedModel:
    name = "fake-model"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def chat(self, system, turns):
        self.seen.append(list(turns))
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return Reply(item, "fake-model", 100, 20)


def test_baseline_loop_runs_one_command_per_turn_and_submits():
    model = ScriptedModel(["THOUGHT: look\n```bash\nls\n```",
                           "oops, forgot the block",
                           f"THOUGHT: done\n```bash\n{SENTINEL}\n```"])
    env = env_with(Budgets())
    BaselineAgent(model).run(env, Observation(text="the issue"))
    assert [s.tool for s in env.steps] == ["bash", None, "submit"]
    assert env.state().done_reason == "submitted"
    assert env.steps[0].metrics == {"prompt_tokens": 100, "completion_tokens": 20}
    assert env.steps[1].observation.startswith("Format error")
    # Linear history: the observation of each command is the next user turn.
    assert model.seen[1][-1][0] == "user" and "<output>" in model.seen[1][-1][1]


def test_blocked_or_cut_off_replies_are_never_executed():
    model = ScriptedModel([ModelResponseError("finishReason MAX_TOKENS"),
                           f"```bash\n{SENTINEL}\n```"])
    env = env_with(Budgets())
    BaselineAgent(model).run(env, Observation(text="issue"))
    assert env.steps[0].tool is None and "could not be used" in env.steps[0].observation
    assert env.steps[-1].tool == "submit"


def test_format_errors_count_against_the_step_budget():
    env = env_with(Budgets(max_steps=3))
    BaselineAgent(ScriptedModel(["no block"] * 5)).run(env, Observation(text="issue"))
    assert env.state().done_reason == "budget:steps" and len(env.steps) == 3


def test_model_outage_propagates_as_an_agent_error():
    env = env_with(Budgets())
    with pytest.raises(ModelUnavailable):
        BaselineAgent(ScriptedModel([ModelUnavailable("quota")])).run(env, Observation(text="i"))


# -- Gemini chat through the audited client --------------------------------------------------


def gemini(text, status=200, finish="STOP", usage=None):
    body = {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": finish}],
            "usageMetadata": usage or {"promptTokenCount": 50, "candidatesTokenCount": 7}}
    return httpx.Response(status, json=body if status == 200 else {})


def chat_client(script, sleeps=None):
    calls = []

    def transport(model, payload):
        calls.append((model, payload))
        return script[model].pop(0)

    client = GeminiClient(SETTINGS, interval=0, transport=lambda *a: None,
                          sleep=(sleeps if sleeps is not None else []).append,
                          rng=random.Random(0))
    client._chat_transport = transport
    return client, calls


def test_gemini_chat_maps_roles_reports_usage_and_retries():
    client, calls = chat_client({"primary-m": [gemini("", 429), gemini("hi")]})
    reply = GeminiChat(client).chat("sys", [("user", "a"), ("assistant", "b"), ("user", "c")])
    assert (reply.text, reply.model, reply.prompt_tokens, reply.completion_tokens) == (
        "hi", "primary-m", 50, 7)
    payload = calls[-1][1]
    assert [c["role"] for c in payload["contents"]] == ["user", "model", "user"]
    assert payload["systemInstruction"]["parts"][0]["text"] == "sys"
    assert payload["generationConfig"]["temperature"] == 0.0
    assert "test-key-not-real" not in repr(payload)


def test_gemini_chat_falls_back_then_gives_up():
    client, calls = chat_client({"primary-m": [gemini("", 503)] * 5, "fallback-m": [gemini("ok")]})
    assert GeminiChat(client).chat("s", [("user", "u")]).model == "fallback-m"
    client, _ = chat_client({"primary-m": [gemini("", 503)] * 5,
                             "fallback-m": [gemini("", 429)] * 5})
    with pytest.raises(ModelUnavailable):
        GeminiChat(client).chat("s", [("user", "u")])


@pytest.mark.parametrize("finish", ["MAX_TOKENS", "SAFETY", "RECITATION"])
def test_gemini_chat_non_stop_is_a_response_error(finish):
    client, _ = chat_client({"primary-m": [gemini("partial ```bash\nrm", finish=finish)]})
    with pytest.raises(ModelResponseError, match=finish):
        GeminiChat(client).chat("s", [("user", "u")])


def test_reply_cost_only_from_a_configured_price():
    assert Reply("x", "gemini-3.5-flash-lite", 10, 5).cost_usd == 0.0
    assert Reply("x", "unpriced-model", 10, 5).cost_usd is None
    assert "cost_usd" not in Reply("x", "unpriced-model", 10, 5).metrics()


# -- OpenAI-compatible provider --------------------------------------------------------------


def openai(text="ok", finish="stop", status=200):
    body = {"choices": [{"message": {"content": text}, "finish_reason": finish}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3,
                      "prompt_tokens_details": {"cached_tokens": 4}}}
    return httpx.Response(status, json=body if status == 200 else {})


def compat(responses, sleeps=None):
    sent = []

    def transport(body):
        sent.append(body)
        return responses.pop(0)

    return OpenAICompatChat("https://example.invalid/v1", "m", "k-not-real", transport=transport,
                            sleep=(sleeps if sleeps is not None else []).append), sent


def test_openai_compat_sends_system_and_turns_and_parses_usage():
    model, sent = compat([openai("hello")])
    reply = model.chat("sys", [("user", "u"), ("assistant", "a"), ("user", "u2")])
    assert reply.text == "hello" and (reply.prompt_tokens, reply.cached_tokens) == (9, 4)
    assert [m["role"] for m in sent[0]["messages"]] == ["system", "user", "assistant", "user"]
    assert "k-not-real" not in repr(model)


def test_openai_compat_retries_then_fails_and_rejects_truncation():
    sleeps = []
    model, _ = compat([openai(status=429), openai(status=503), openai("ok")], sleeps)
    assert model.chat("s", [("user", "u")]).text == "ok" and len(sleeps) == 2
    model, _ = compat([openai("cut", finish="length")])
    with pytest.raises(ModelResponseError, match="length"):
        model.chat("s", [("user", "u")])
    model, _ = compat([openai(status=401)])
    with pytest.raises(ModelUnavailable, match="401"):
        model.chat("s", [("user", "u")])


def test_openai_compat_settings_come_from_env(monkeypatch, tmp_path):
    monkeypatch.setattr("repogym.agents.models.ROOT", tmp_path)
    for name in ("OPENAI_COMPAT_BASE_URL", "OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_MODEL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ModelUnavailable, match="OPENAI_COMPAT_API_KEY"):
        OpenAICompatChat.from_env()
