"""Tool-calling providers with fake transports: signatures, usage, retries, errors (no network)."""

import base64

import httpx
import pytest
from google.genai import errors, types

from repogym.providers.base import (
    BadResponse,
    DailyQuotaExhausted,
    Message,
    ProviderUnavailable,
    RequestRejected,
    ToolCall,
    ToolResult,
)
from repogym.providers.gemini import GeminiProvider, GeminiSettings, from_content, to_contents
from repogym.providers.openai_compat import OpenAICompatProvider
from repogym.providers.retry import RetryPolicy, retry_info
from repogym.tools.definitions import TOOLS

SIG = b"\x01opaque-signature\xff"


def response(parts, finish="STOP", prompt=120, thoughts=33, cached=40):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(content=types.Content(role="model", parts=parts), finish_reason=finish)
        ],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            candidates_token_count=12,
            thoughts_token_count=thoughts,
            cached_content_token_count=cached,
            total_token_count=prompt + 12 + thoughts,
        ),
        model_version="gemma-4-26b-a4b-it",
    )


def call_part(name="bash", args=None, sig=SIG, cid="c1"):
    return types.Part(
        function_call=types.FunctionCall(id=cid, name=name, args=args or {"command": "ls"}),
        thought_signature=sig,
    )


class FakeModels:
    def __init__(self, script):
        self.script, self.requests = list(script), []

    def generate_content(self, model, contents, config):
        self.requests.append({"model": model, "contents": contents, "config": config})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.models = FakeModels(script)


class RecordingLimiter:
    def __init__(self):
        self.acquired, self.settled, self.daily = [], [], 0

    def acquire(self, estimated_tokens):
        self.acquired.append(estimated_tokens)

    def settle(self, prompt_tokens):
        self.settled.append(prompt_tokens)

    def mark_daily_exhausted(self):
        self.daily += 1
        return 12345.0


def provider(script, sleeps=None, limiter=None, settings=None, policy=None):
    sleeps = [] if sleeps is None else sleeps
    return GeminiProvider(
        settings or GeminiSettings(model="gemma-4-26b-a4b-it"),
        api_key="unused",
        client=FakeClient(script),
        sleep=sleeps.append,
        limiter=limiter or RecordingLimiter(),
        policy=policy or RetryPolicy(max_attempts=4, base_delay=1.0, server_max_failures=4),
    )


def api_error(code, body):
    return errors.APIError(code, body)


def rate_limited(delay="20s", quota="GenerateRequestsPerMinutePerProjectPerModel-FreeTier"):
    return api_error(
        429,
        {
            "error": {
                "code": 429,
                "status": "RESOURCE_EXHAUSTED",
                "details": [
                    {
                        "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                        "violations": [{"quotaId": quota}],
                    },
                    {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay},
                ],
            }
        },
    )


# -- message mapping and signatures ----------------------------------------------------------


def test_reply_keeps_calls_text_and_signature_and_replays_byte_identically():
    reply = types.Content(
        role="model",
        parts=[
            types.Part(text="Let me look.", thought_signature=b"text-sig"),
            call_part(sig=SIG),
            call_part("view_file", {"path": "a.py"}, sig=None, cid="c2"),
        ],
    )
    message = from_content(reply)
    assert message.text == "Let me look." and [c.name for c in message.calls] == [
        "bash",
        "view_file",
    ]
    assert message.calls[0].id == "c1" and message.calls[1].args == {"path": "a.py"}
    replayed = to_contents([message])[0]
    assert (
        replayed.parts[1].thought_signature == SIG
        and replayed.parts[0].thought_signature == b"text-sig"
    )
    assert replayed.parts[2].thought_signature is None  # parallel calls: only the first is signed
    assert replayed.model_dump(exclude_none=True) == reply.model_dump(exclude_none=True)


def test_tool_results_map_to_function_responses_with_matching_id_and_name():
    contents = to_contents(
        [
            Message(role="user", text="task", extra_text=["summary"]),
            Message(role="tool", results=[ToolResult("c1", "bash", "out")]),
        ]
    )
    assert [p.text for p in contents[0].parts] == ["task", "summary"]
    fr = contents[1].parts[0].function_response
    assert (fr.id, fr.name, fr.response) == ("c1", "bash", {"output": "out"})


def test_generate_parses_usage_and_sends_tools_without_sdk_retries():
    limiter = RecordingLimiter()
    p = provider(
        [response([call_part()])],
        limiter=limiter,
        settings=GeminiSettings(
            model="m", sampling={"temperature": 1.0, "top_p": 0.95}, thinking_level="high"
        ),
    )
    g = p.generate("system", [Message(role="user", text="task")], TOOLS, estimated_tokens=99)
    assert (
        g.usage.prompt_tokens == 120
        and g.usage.thinking_tokens == 33
        and g.usage.cached_tokens == 40
    )
    assert g.finish_reason == "STOP" and g.model_version == "gemma-4-26b-a4b-it" and g.retries == 0
    cfg = p.client.models.requests[0]["config"]
    assert cfg.automatic_function_calling.disable is True and cfg.temperature == 1.0
    assert cfg.thinking_config.thinking_level.name.lower() == "high"
    assert cfg.http_options.retry_options.attempts == 1
    assert [d.name for d in cfg.tools[0].function_declarations] == [t["name"] for t in TOOLS]
    assert limiter.acquired == [99] and limiter.settled == [120]


def test_signature_from_one_call_is_sent_back_on_the_next():
    p = provider([response([call_part()]), response([types.Part(text="done")])])
    first = p.generate("s", [Message(role="user", text="task")], TOOLS)
    history = [
        Message(role="user", text="task"),
        first.message,
        Message(role="tool", results=[ToolResult("c1", "bash", "file.py")]),
    ]
    p.generate("s", history, TOOLS)
    sent = p.client.models.requests[1]["contents"][1]
    assert sent.role == "model" and sent.parts[0].thought_signature == SIG


# -- retries and errors ----------------------------------------------------------------------


def test_per_minute_429_waits_at_least_the_server_delay_then_succeeds():
    sleeps, limiter = [], RecordingLimiter()
    p = provider([rate_limited("20s"), response([types.Part(text="ok")])], sleeps, limiter)
    g = p.generate("s", [Message(role="user", text="t")], TOOLS)
    assert sleeps and sleeps[0] >= 20 and g.retries == 1 and g.attempts[0].kind == "rate_limited"
    assert len(limiter.acquired) == 2  # every attempt goes through the limiter


def test_daily_quota_429_stops_at_once_and_marks_the_limiter():
    limiter, sleeps = RecordingLimiter(), []
    p = provider(
        [rate_limited("40000s", "GenerateRequestsPerDayPerProjectPerModel-FreeTier")],
        sleeps,
        limiter,
    )
    with pytest.raises(DailyQuotaExhausted) as info:
        p.generate("s", [Message(role="user", text="t")], TOOLS)
    assert sleeps == [] and limiter.daily == 1 and info.value.reset_at == 12345.0


def test_rejected_and_unavailable_and_bad_finish():
    with pytest.raises(RequestRejected, match="400"):
        provider([api_error(400, {"error": {"status": "INVALID_ARGUMENT"}})]).generate(
            "s", [Message(role="user", text="t")], TOOLS
        )
    with pytest.raises(ProviderUnavailable) as info:
        provider([api_error(500, {})] * 4).generate("s", [Message(role="user", text="t")], TOOLS)
    assert [a.kind for a in info.value.attempts] == ["server_error"] * 4
    with pytest.raises(BadResponse, match="MAX_TOKENS"):
        provider([response([types.Part(text="cut")], finish="MAX_TOKENS")]).generate(
            "s", [Message(role="user", text="t")], TOOLS
        )


def test_retry_info_parses_delay_and_quota_ids():
    body = rate_limited("7.5s").details
    assert retry_info(body) == (7.5, ["GenerateRequestsPerMinutePerProjectPerModel-FreeTier"])
    assert retry_info({}) == (None, [])


def test_summary_generation_accepts_max_tokens_cutoff():
    p = provider([response([types.Part(text="summary so far")], finish="MAX_TOKENS")])
    assert p.generate_text("s", "summarise", 100).message.text == "summary so far"


def test_assistant_message_from_another_provider_is_refused():
    with pytest.raises(ValueError):
        to_contents(
            [
                Message(
                    role="assistant", text="x", raw={"role": "assistant"}, provider="openai-compat"
                )
            ]
        )


# -- OpenAI-compatible -----------------------------------------------------------------------


def openai_body(tool_calls=None, finish="tool_calls", content=None):
    return {
        "model": "qwen",
        "choices": [
            {
                "finish_reason": finish,
                "message": {"role": "assistant", "content": content, "tool_calls": tool_calls},
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 7,
            "total_tokens": 57,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }


def test_openai_compat_tool_calls_roundtrip_and_errors():
    sent = []

    def transport(body):
        sent.append(body)
        return httpx.Response(
            200,
            json=openai_body(
                [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": '{"command": "ls"}'},
                    }
                ]
            ),
        )

    p = OpenAICompatProvider(
        "qwen",
        "http://localhost:11434/v1",
        api_key="k-secret",
        transport=transport,
        sleep=lambda s: None,
    )
    g = p.generate("sys", [Message(role="user", text="task")], TOOLS)
    assert g.message.calls[0].args == {"command": "ls"} and g.usage.thinking_tokens == 3
    p.generate(
        "sys",
        [
            Message(role="user", text="task"),
            g.message,
            Message(role="tool", results=[ToolResult("t1", "bash", "out")]),
        ],
        TOOLS,
    )
    roles = [m["role"] for m in sent[1]["messages"]]
    assert (
        roles == ["system", "user", "assistant", "tool"]
        and sent[1]["messages"][3]["tool_call_id"] == "t1"
    )
    assert "k-secret" not in repr(p)
    bad = OpenAICompatProvider(
        "q",
        "http://x/v1",
        transport=lambda b: httpx.Response(200, json=openai_body(finish="length", content="cut")),
        sleep=lambda s: None,
    )
    with pytest.raises(BadResponse, match="length"):
        bad.generate("s", [Message(role="user", text="t")], TOOLS)


def test_signature_bytes_survive_json_storage():
    message = from_content(types.Content(role="model", parts=[call_part(sig=SIG)]))
    import json

    restored = Message(**{**message.__dict__, "raw": json.loads(json.dumps(message.raw))})
    assert to_contents([restored])[0].parts[0].thought_signature == SIG
    stored = message.raw["parts"][0]["thought_signature"]  # the SDK stores URL-safe base64
    assert base64.urlsafe_b64decode(stored + "=" * (-len(stored) % 4)) == SIG


# -- D17 waits and D13 countTokens -----------------------------------------------------------


class WaitingLimiter(RecordingLimiter):
    def acquire(self, estimated_tokens):
        super().acquire(estimated_tokens)
        return 7.5  # held this long by the rate limiter


def test_limiter_waits_and_backoff_are_reported_separately_from_api_time():
    sleeps = []
    p = provider([rate_limited("20s"), response([types.Part(text="ok")])], sleeps, WaitingLimiter())
    g = p.generate("s", [Message(role="user", text="t")], TOOLS)
    assert g.limiter_wait_seconds == 15.0  # two attempts, 7.5 s each
    assert g.backoff_seconds == pytest.approx(sleeps[0]) and g.backoff_seconds >= 20
    assert g.wait_seconds == pytest.approx(15.0 + sleeps[0])
    assert g.latency_seconds >= 0.0  # fake sleeps take no real time, so API time is ~0


def test_bad_response_carries_its_waits():
    p = provider([response([types.Part(text="x")], finish="SAFETY")], limiter=WaitingLimiter())
    with pytest.raises(BadResponse) as exc:
        p.generate("s", [Message(role="user", text="t")], TOOLS)
    assert exc.value.wait_seconds == 7.5


class FakeHttp:
    def __init__(self, status=200, payload=None):
        self.status, self.payload, self.posts = status, payload or {"totalTokens": 505}, []

    def post(self, url, headers, json):
        self.posts.append((url, headers, json))
        status, payload = self.status, self.payload
        return type("R", (), {"status_code": status, "json": lambda self: payload})()


def test_count_tokens_counts_the_whole_request_via_generate_content_request():
    http = FakeHttp()
    p = provider([])
    p._http = http
    reply = Message(
        role="assistant",
        calls=[ToolCall(id="c1", name="bash", args={"command": "ls"})],
        provider="gemini",
        raw=response([call_part()])
        .candidates[0]
        .content.model_dump(mode="json", exclude_none=True),
    )
    history = [Message(role="user", text="task"), reply]
    assert p.count_tokens("system text", history, TOOLS) == 505
    url, headers, body = http.posts[0]
    assert url.endswith("/gemma-4-26b-a4b-it:countTokens") and headers["x-goog-api-key"] == "unused"
    request = body["generateContentRequest"]
    assert set(body) == {"generateContentRequest"}  # never contents + request together
    assert request["systemInstruction"]["parts"][0]["text"] == "system text"
    assert request["tools"][0]["functionDeclarations"]
    assert "thoughtSignature" in request["contents"][1]["parts"][0]  # counted as sent
    p._http = FakeHttp(status=400)
    assert p.count_tokens("s", history, TOOLS) is None  # the estimator is the fallback
    foreign = Message(role="assistant", text="x", raw={"x": 1}, provider="openai")
    assert p.count_tokens("s", [history[0], foreign], TOOLS) is None  # never raises


def test_replies_with_thought_text_carry_their_replay_cost():
    """Gemma returns its thinking as a thought part, billed twice when replayed (D21)."""
    thought = types.Part(text="let me think", thought=True)
    p = provider([response([thought, call_part()], thoughts=120), response([call_part()])])
    g = p.generate("s", [Message(role="user", text="t")], TOOLS)
    assert g.message.replay_tokens == 240 and "let me think" not in g.message.text
    plain = p.generate("s", [Message(role="user", text="t")], TOOLS)
    assert plain.message.replay_tokens == 0  # no thought part: nothing extra is replayed


# -- D16 (amended): server errors pause in place; daily quota holds the call ----------------


from repogym.providers.base import Generation  # noqa: E402
from repogym.providers.retry import Outcome, run_with_retries  # noqa: E402


class Clock:
    """Fake monotonic and wall clocks that advance only when something sleeps."""

    def __init__(self):
        self.t, self.sleeps = 1_800_000_000.0, []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def scripted(statuses):
    queue = list(statuses)

    def send():
        status = queue.pop(0)
        return Outcome(ok=True, status=200, value="ok") if status == 200 else Outcome(False, status)

    return send


def retry(send, clock, policy=None, **kw):
    import random as _random

    return run_with_retries(
        send,
        policy=policy or RetryPolicy(),
        model="m",
        sleep=clock.sleep,
        clock=clock,
        wall=clock,
        rng=_random.Random(0),
        **kw,
    )


def test_server_errors_are_retried_in_place_with_capped_jittered_backoff():
    clock = Clock()
    value, attempts = retry(scripted([500] * 6 + [503] * 3 + [200]), clock)  # 9 failures
    assert value == "ok" and len(attempts) == 10 and attempts[-1].kind == "ok"
    assert 15 <= clock.sleeps[0] <= 45  # starts around 30 s, with jitter
    assert max(clock.sleeps) <= 600  # never more than 10 minutes between tries
    assert all(a.at is not None for a in attempts)


def test_twenty_consecutive_server_errors_give_up():
    clock = Clock()
    policy = RetryPolicy(server_max_seconds=10**9)  # only the count can end it
    with pytest.raises(ProviderUnavailable, match="20 consecutive server errors") as info:
        retry(scripted([500] * 25), clock, policy)
    assert len(info.value.attempts) == 20 and info.value.attempts[-1].slept == 0.0


def test_sixty_minutes_of_server_errors_give_up_first():
    clock = Clock()
    with pytest.raises(ProviderUnavailable) as info:
        retry(scripted([500] * 25), clock)
    # With 30 s doubling to a 10-minute cap, 60 minutes are used up after ~10-11 failures:
    # at these settings the time limit always comes before the 20-failure limit.
    assert 10 <= len(info.value.attempts) <= 12
    failed_for = clock.t - info.value.attempts[0].at
    assert failed_for == pytest.approx(3600.0)  # the waits are trimmed to the 60-minute budget


def test_rate_limits_and_client_errors_keep_their_own_rules():
    clock = Clock()
    with pytest.raises(ProviderUnavailable, match="rate limited 6 times"):
        retry(lambda: Outcome(False, 429, body={}), clock)
    with pytest.raises(RequestRejected):
        retry(scripted([500, 400]), Clock())  # a 4xx after a 5xx is still rejected at once


def test_daily_quota_holds_the_call_until_the_reset_then_continues():
    clock = Clock()
    reset = clock.t + 5 * 3600
    calls = {"n": 0}

    def before():  # the limiter: today's quota is used up, once
        calls["n"] += 1
        if calls["n"] == 1:
            raise DailyQuotaExhausted("used up", reset_at=reset)

    policy = RetryPolicy(pause_on_daily=True)
    value, attempts = retry(scripted([200]), clock, policy, before_attempt=before)
    assert value == "ok" and clock.sleeps == [pytest.approx(5 * 3600 + 60)]
    assert [a.kind for a in attempts] == ["daily_quota_pause", "ok"]
    gen_like = Generation(
        message=None,
        usage=None,
        finish_reason=None,
        model_version=None,
        latency_seconds=0,
        http_status=200,
        attempts=attempts,
    )
    assert gen_like.quota_pause_seconds == pytest.approx(5 * 3600 + 60)
    assert gen_like.wait_seconds >= gen_like.quota_pause_seconds


def test_a_daily_429_also_pauses_but_only_once_per_call():
    clock = Clock()
    body = {
        "error": {"details": [{"violations": [{"quotaId": "GenerateRequestsPerDayPerProject"}]}]}
    }
    queue = [Outcome(False, 429, body=body), Outcome(False, 429, body=body)]
    policy = RetryPolicy(pause_on_daily=True)
    with pytest.raises(DailyQuotaExhausted):
        retry(lambda: queue.pop(0), clock, policy, on_daily=lambda: clock.t + 100)
    assert len(clock.sleeps) == 1  # paused once, then gave up instead of pausing forever
    without = Clock()
    with pytest.raises(DailyQuotaExhausted):
        retry(lambda: Outcome(False, 429, body=body), without, on_daily=lambda: without.t + 100)
    assert without.sleeps == []  # pausing is off by default


def test_every_attempt_is_streamed_to_the_attempt_log():
    seen = []
    retry(scripted([500, 503, 200]), Clock(), on_attempt=seen.append)
    assert [a.status for a in seen] == [500, 503, 200] and all(a.at for a in seen)


def test_attempt_log_numbers_each_calls_attempts_per_utc_day(tmp_path):
    import json as _json

    from repogym.ratelimit import AttemptLog

    log = AttemptLog(tmp_path)
    p = provider([api_error(500, {}), response([types.Part(text="ok")])])
    p.attempt_log = log
    p.generate("s", [Message(role="user", text="t")], TOOLS)
    files = list(tmp_path.glob("attempts-*.jsonl"))
    rows = [_json.loads(x) for x in files[0].read_text().splitlines()]
    assert len(files) == 1 and [(r["attempt"], r["status"]) for r in rows] == [(1, 500), (2, 200)]
    assert all(r["model"] == "gemma-4-26b-a4b-it" and r["at"] for r in rows)
