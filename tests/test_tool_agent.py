"""The function-calling agent, with a fake provider and a fake sandbox (no network, no Docker)."""

import pytest

from repogym import accounting, trajectory
from repogym.agents.tool_agent import NO_TOOL_NUDGE, ToolAgent
from repogym.context.base import ContextConfig, ContextOverflow
from repogym.context.strategies import ContextManager
from repogym.context.tokens import TokenEstimator
from repogym.env.budgets import Budgets
from repogym.env.environment import Observation
from repogym.harness import EpisodeRecord, termination_of
from repogym.providers.base import (
    Attempt,
    BadResponse,
    DailyQuotaExhausted,
    Generation,
    Message,
    ToolCall,
    Usage,
)
from tests.test_episode import env_with


def reply(*calls, text="thinking"):
    tool_calls = [ToolCall(id=f"id{i}", name=n, args=a) for i, (n, a) in enumerate(calls)]
    return Message(
        role="assistant",
        text=text,
        calls=tool_calls,
        raw={"role": "model", "parts": [{"text": text}]},
        provider="fake",
    )


class FakeProvider:
    def __init__(self, script, name="gemma-4-26b-a4b-it"):
        self.script, self.name, self.seen, self.summaries = list(script), name, [], []

    def generate(self, system, messages, tools, estimated_tokens=0):
        self.seen.append([m.copy() for m in messages])
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return Generation(
            message=item,
            usage=Usage(
                prompt_tokens=1200,
                cached_tokens=200,
                output_tokens=30,
                thinking_tokens=70,
                total_tokens=1300,
            ),
            finish_reason="STOP",
            model_version=self.name,
            latency_seconds=0.5,
            http_status=200,
            attempts=[Attempt(429, "rate_limited", 20.0, 20.0), Attempt(200, "ok")],
        )

    def generate_text(self, system, prompt, max_tokens, estimated_tokens=0):
        self.summaries.append(prompt)
        return Generation(
            message=Message(role="assistant", text="SUMMARY"),
            usage=Usage(prompt_tokens=500, output_tokens=50, thinking_tokens=0),
            finish_reason="STOP",
            model_version=self.name,
            latency_seconds=0.2,
            http_status=200,
            attempts=[Attempt(200, "ok")],
        )

    def count_tokens(self, system, messages, tools):
        return None


class EnvFacade:
    """Only the environment API. Any other attribute access fails the test."""

    def __init__(self, env):
        self._env = env

    def step(self, action):
        return self._env.step(action)

    def note(self, *args, **kwargs):
        return self._env.note(*args, **kwargs)

    def state(self):
        return self._env.state()

    def record_event(self, *args, **kwargs):
        return self._env.record_event(*args, **kwargs)

    def record_wait(self, seconds):
        return self._env.record_wait(seconds)

    def record_quota_pause(self, seconds):
        return self._env.record_quota_pause(seconds)


def agent(script, strategy="obs_mask", budget=12_000, system_prompt=None, **kw):
    cm = ContextManager(strategy, ContextConfig(budget=budget, **kw), TokenEstimator())
    return ToolAgent(FakeProvider(script), cm, system_prompt=system_prompt)


def run(tool_agent, budgets=None):
    env = env_with(budgets or Budgets())
    tool_agent.run(EnvFacade(env), Observation(text="Fix the issue: X is broken."))
    return env


def test_tool_calls_go_through_env_step_only_and_results_feed_the_next_prompt():
    a = agent(
        [
            reply(("bash", {"command": "ls"})),
            reply(("create_file", {"path": "a.py", "content": "x = 1\n"})),
            reply(("submit", {})),
        ]
    )
    env = run(a)
    assert [s.tool for s in env.steps] == ["bash", "create_file", "submit"]
    assert env.state().done_reason == "submitted"
    second_prompt = a.provider.seen[1]
    assert (
        second_prompt[0].text == "Fix the issue: X is broken." and second_prompt[-1].role == "tool"
    )
    assert second_prompt[-1].results[0].call_id == "id0"


def test_parallel_calls_are_separate_steps_with_metrics_on_the_first_only():
    env = run(
        agent(
            [reply(("bash", {"command": "a"}), ("bash", {"command": "b"})), reply(("submit", {}))]
        )
    )
    first, second = env.steps[0], env.steps[1]
    assert (
        first.metrics and first.llm_calls == 1 and second.metrics is None and second.llm_calls == 0
    )
    extra = first.metrics["extra"]["repogym"]
    assert first.metrics["completion_tokens"] == 100  # output + thinking (ATIF semantics)
    assert extra["rate_limited"] == 1 and extra["retries"] == 1 and extra["budget"] == 12_000
    assert extra["view"]["units"] == {} and extra["budget_violation"] is False


def test_no_tool_reply_is_a_counted_nudge_and_bad_response_is_never_acted_on():
    a = agent(
        [
            reply(text="I think the bug is in x."),
            BadResponse("finish_reason MAX_TOKENS"),
            reply(("submit", {})),
        ]
    )
    env = run(a)
    assert [s.tool for s in env.steps] == [None, None, "submit"]
    assert env.steps[0].observation == NO_TOOL_NUDGE and "MAX_TOKENS" in env.steps[1].observation
    assert a.provider.seen[1][-1].text.startswith(NO_TOOL_NUDGE)  # the nudge is in the history
    assert a.provider.seen[1][-1].text.endswith("[step 1/100]")
    assert len(a.provider.seen[2]) == len(a.provider.seen[1])  # the bad reply added nothing


def test_budget_violation_is_flagged_when_reported_tokens_exceed_b():
    # The estimate (~1,086) fits B=1,100, but the API reports 1,200 prompt tokens: a violation.
    env = run(agent([reply(("submit", {}))], budget=1_100, system_prompt="Fix it."))
    assert env.steps[0].metrics["extra"]["repogym"]["budget_violation"] is True


def test_summary_calls_are_recorded_as_events_with_their_metrics():
    big = "y" * 9_000
    script = [reply(("bash", {"command": f"cat f{i}"})) for i in range(6)] + [reply(("submit", {}))]
    a = agent(script, strategy="llm_summary", budget=6_000, keep_recent_units=2)
    env = env_with(Budgets())
    env.sandbox.output = big
    a.run(EnvFacade(env), Observation(text="task"))
    summaries = [e for e in env.events if e.kind == "summary"]
    assert (
        summaries
        and summaries[0].message == "SUMMARY"
        and summaries[0].metrics["prompt_tokens"] == 500
    )
    assert a.provider.summaries and "Result of bash" in a.provider.summaries[0]
    acct = accounting.summarize(env.steps, env.events, a.model_name)
    assert acct["summary_overhead"]["calls"] == len(summaries) and acct["cost_usd"] is None  # Gemma


def test_context_overflow_and_quota_errors_map_to_terminations():
    with pytest.raises(ContextOverflow):
        run(agent([reply(("submit", {}))], budget=100))
    with pytest.raises(DailyQuotaExhausted):
        run(agent([DailyQuotaExhausted("daily")]))
    base = dict(
        run_id="r",
        episode_id="e",
        task_id="t",
        agent="a",
        agent_version="1",
        status="agent_error",
        started_at="x",
        release="v0.1",
        tasks_sha256="s",
        image="i",
        image_digest="d",
        repogym_commit="c",
        repogym_dirty=False,
        budgets=Budgets(),
    )
    assert termination_of(EpisodeRecord(**base, error_type="ContextOverflow")) == "context_failure"
    assert termination_of(EpisodeRecord(**base, error_type="DailyQuotaExhausted")) == "api_failure"
    assert termination_of(EpisodeRecord(**base, error_type="KeyError")) == "harness_error"
    graded = {**base, "status": "graded"}
    assert termination_of(EpisodeRecord(**graded, done_reason="submitted")) == "submitted"
    assert termination_of(EpisodeRecord(**graded, done_reason="budget:steps")) == "step_cap"
    assert (
        termination_of(EpisodeRecord(**graded, done_reason="budget:wall_clock")) == "wallclock_cap"
    )
    assert termination_of(EpisodeRecord(**graded, done_reason="budget:disk")) == "harness_error"
    # The elapsed safety cap only fires when throttling stalls an episode: an API failure (D17).
    assert termination_of(EpisodeRecord(**graded, done_reason="budget:elapsed")) == "api_failure"
    assert termination_of(EpisodeRecord(**{**base, "status": "setup_error"})) == "harness_error"


def test_flash_lite_cost_uses_list_prices():
    a = agent([reply(("submit", {}))])
    a.provider.name = "gemini-3.5-flash-lite"
    env = run(a)
    acct = accounting.summarize(env.steps, env.events, "gemini-3.5-flash-lite")
    # (1200-200)*0.30 + 200*0.03 + (30+70)*2.50, per million tokens
    assert acct["cost_usd"] == pytest.approx((1000 * 0.30 + 200 * 0.03 + 100 * 2.50) / 1e6)
    assert acct["input_tokens"] == 1200 and acct["thinking_tokens"] == 70 and acct["requests"] == 2


def test_trajectory_with_context_events_is_valid_atif():
    a = agent(
        [reply(("bash", {"command": "cat big"})), reply(("submit", {}))],
        budget=12_000,
        clip_tokens=200,
    )
    env = env_with(Budgets())
    env.sandbox.output = "z" * 20_000
    a.run(EnvFacade(env), Observation(text="task"))
    assert any(e.kind == "clip" for e in env.events)
    doc = trajectory.build(
        episode_id="e",
        session_id="s",
        agent_name=a.name,
        agent_version="1",
        model_name=a.model_name,
        system_text="sys",
        user_text="task",
        started_at="2026-11-09T00:00:00Z",
        steps=env.steps,
        events=env.events,
        final_extra={},
        root_extra={},
    )
    assert trajectory.validate(doc) == []
    system_steps = [s for s in doc["steps"] if s["source"] == "system"][1:]
    assert system_steps[0]["extra"]["context_management"] == {
        "type": "pruning",
        "boundary": "truncate",
    }


def test_history_keeps_full_observations_even_when_the_prompt_clips_them():
    a = agent([reply(("bash", {"command": "cat big"})), reply(("submit", {}))], clip_tokens=200)
    # Week 3 budgets: the environment's storage cap is far above the prompt clip (D14).
    env = env_with(Budgets(max_observation_chars=200_000))
    env.sandbox.output = "w" * 20_000
    a.run(EnvFacade(env), Observation(text="task"))
    assert "w" * 20_000 in env.steps[0].observation  # stored in full (D14)
    shown = a.provider.seen[1][-1].results[0].output
    assert "clipped by RepoGym" in shown and len(shown) < 2_000  # the model saw a clip


# -- D17: the active-time cap excludes throttling; a separate elapsed cap catches hangs ------


class FakeClock:
    def __init__(self):
        self.t = 1_000.0

    def __call__(self):
        return self.t


def throttled_run(script, budgets, wait, api_seconds):
    """Each call is held ``wait`` s by the limiter, then spends ``api_seconds`` on the API."""
    clock = FakeClock()
    a = agent(script)
    inner = a.provider.generate

    def generate(*args, **kwargs):
        gen = inner(*args, **kwargs)
        gen.limiter_wait_seconds = wait
        gen.latency_seconds = api_seconds
        clock.t += gen.wait_seconds + api_seconds  # limiter + backoff (20 s) + API time
        return gen

    a.provider.generate = generate
    env = env_with(budgets)
    env._clock, env._t0 = clock, clock()
    a.run(EnvFacade(env), Observation(text="Fix the issue: X is broken."))
    return a, env


def test_long_limiter_waits_never_trigger_the_active_time_cap():
    script = [reply(("bash", {"command": "ls"})) for _ in range(20)] + [reply(("submit", {}))]
    budgets = Budgets(max_wall_seconds=600, max_elapsed_seconds=None)
    a, env = throttled_run(script, budgets, wait=3_000.0, api_seconds=1.0)
    state = env.state()
    assert state.done_reason == "submitted" and state.step_count == 21
    assert state.elapsed_seconds == pytest.approx(21 * 3_021.0)  # ~17.6 h, almost all waiting
    assert state.waiting_seconds == pytest.approx(21 * 3_020.0)
    assert state.active_seconds == pytest.approx(21.0)
    extra = env.steps[0].metrics["extra"]["repogym"]
    assert extra["limiter_wait_seconds"] == 3_000.0 and extra["backoff_seconds"] == 20.0
    assert extra["waiting_seconds"] == 3_020.0
    acct = accounting.summarize(env.steps, env.events, a.model_name)
    assert acct["waiting_seconds"] == pytest.approx(21 * 3_020.0)
    assert acct["api_seconds"] == pytest.approx(21.0)


def test_active_time_still_caps_slow_episodes_without_throttling():
    script = [reply(("bash", {"command": "ls"})) for _ in range(20)] + [reply(("submit", {}))]
    _, env = throttled_run(script, Budgets(max_wall_seconds=600), wait=0.0, api_seconds=100.0)
    state = env.state()
    assert state.done_reason == "budget:wall_clock"
    assert state.active_seconds >= 600 and state.step_count < 21


def test_elapsed_safety_cap_ends_a_stalled_episode_as_an_api_failure():
    script = [reply(("bash", {"command": "ls"})) for _ in range(20)] + [reply(("submit", {}))]
    budgets = Budgets(max_wall_seconds=600, max_elapsed_seconds=6 * 3600)
    _, env = throttled_run(script, budgets, wait=3_000.0, api_seconds=1.0)
    state = env.state()
    assert state.done_reason == "budget:elapsed" and state.active_seconds < 600
    assert state.step_count == 7  # the 8th call crosses 6 h (8 x 3,021 s); its action is refused


def test_waits_inside_a_bad_response_are_excluded_too():
    clock = FakeClock()
    exc = BadResponse("finish_reason SAFETY")
    exc.wait_seconds = 5_000.0
    a = agent([exc, reply(("submit", {}))])
    inner = a.provider.generate

    def generate(*args, **kwargs):
        if a.provider.script and isinstance(a.provider.script[0], BadResponse):
            clock.t += 5_000.0
        return inner(*args, **kwargs)

    a.provider.generate = generate
    env = env_with(Budgets(max_wall_seconds=600))
    env._clock, env._t0 = clock, clock()
    a.run(EnvFacade(env), Observation(text="Fix the issue: X is broken."))
    assert env.state().done_reason == "submitted"
    assert env.state().waiting_seconds >= 5_000.0


def test_summary_omitted_events_are_counted_and_typed_in_the_trajectory():
    from repogym.env.environment import EventRecord

    env = run(agent([reply(("bash", {"command": "ls"})), reply(("submit", {}))]))
    env.events.append(
        EventRecord(after_step=1, at="x", kind="summary_omitted", detail={"summary_covers": 3})
    )
    acct = accounting.summarize(env.steps, env.events, "gemma-4-26b-a4b-it")
    assert acct["summary_omitted"] == 1 and acct["summary_omitted_rate"] == 0.5
    assert trajectory.CONTEXT_TYPES["summary_omitted"] == ("pruning", "truncate")


# -- D19: reasoning, view description, step counter --------------------------------------------


def test_system_prompt_describes_exactly_what_each_strategy_shows():
    from repogym.agents.tool_agent import system_prompt_for

    cfg = ContextConfig(clip_tokens=2000, keep_observations=5)
    masked = system_prompt_for("obs_mask", cfg, 75)
    assert "last 5 tool calls" in masked and "about 2000 tokens" in masked
    assert "[step k/75]" in masked and "summary" not in masked
    window = system_prompt_for("sliding_window", cfg, 75)
    assert "oldest steps" in window and "placeholder" not in window
    hybrid = system_prompt_for("hybrid", cfg, 75)
    assert "placeholder" in hybrid and "summary written for you" in hybrid
    for text in (masked, window, hybrid):
        assert "200000" not in text and "write one or two sentences" in text


def test_step_counter_and_one_final_nudge_follow_tool_results():
    script = [reply(("bash", {"command": f"echo {i}"})) for i in range(6)] + [reply(("submit", {}))]
    a = agent(script)
    env = run(a, Budgets(max_steps=20))  # the nudge comes when 15 calls remain: from step 5
    shown = [m.results[-1].output for p in a.provider.seen[1:] for m in p[-1:]]
    assert shown[0].endswith("[step 1/20]") and shown[4].splitlines()[-2] == "[step 5/20]"
    nudges = [e for e in env.events if e.kind == "final_steps_nudge"]
    assert len(nudges) == 1 and nudges[0].detail == {"step": 5}
    assert sum("If you haven't fixed the issue yet" in t for t in shown) == 1
    assert "[step" not in env.steps[0].observation  # the stored observation is unannotated


def test_missing_reasoning_is_recorded_not_rejected_and_reminded_every_second_time():
    bare = [reply(("bash", {"command": f"echo {i}"}), text="") for i in range(4)]
    a = agent([*bare, reply(("submit", {}))])
    env = run(a)
    assert [s.tool for s in env.steps] == ["bash"] * 4 + ["submit"]  # every call still ran
    missing = [e.detail["streak"] for e in env.events if e.kind == "missing_reasoning"]
    assert missing == [1, 2, 3, 4]
    assert [e.detail["step"] for e in env.events if e.kind == "reasoning_reminder"] == [2, 4]
    reminded = [p[-1].results[-1].output for p in a.provider.seen[1:]]
    assert ["Reminder:" in t for t in reminded] == [False, True, False, True]
    acct = accounting.summarize(env.steps, env.events, a.model_name)
    assert acct["missing_reasoning"] == 4 and acct["missing_reasoning_rate"] == 0.8
    assert acct["reasoning_reminders"] == 2


def test_reasoning_resets_the_streak():
    script = [
        reply(("bash", {"command": "a"}), text=""),
        reply(("bash", {"command": "b"})),  # has reasoning
        reply(("bash", {"command": "c"}), text=""),
        reply(("submit", {})),
    ]
    env = run(agent(script))
    assert [e.detail["streak"] for e in env.events if e.kind == "missing_reasoning"] == [1, 1]
    assert not [e for e in env.events if e.kind == "reasoning_reminder"]


def test_progress_hook_sees_every_model_call():
    seen = []
    a = agent([reply(("bash", {"command": "ls"})), reply(text="hmm"), reply(("submit", {}))])
    a.progress = seen.append
    run(a)
    assert seen == [1, 2, 3]  # after every model call, including the final submit


def test_first_message_for_the_tool_agent_states_active_time_not_storage_cap():
    from repogym.env.environment import instructions
    from tests.test_episode import _task

    task = _task("t", "2024-01-01T00:00:00Z", "dev")
    budgets = Budgets(max_observation_chars=200_000)
    week2 = instructions(task, budgets)
    week3 = instructions(task, budgets, "tool_agent")
    assert "truncated to 200000 characters" in week2  # Week 2 text unchanged
    assert "200000" not in week3 and "active time" in week3
    assert ToolAgent.instruction_style == "tool_agent"


def test_replayed_thinking_is_counted_exactly_in_the_estimate():
    est = TokenEstimator()
    plain = Message(role="assistant", text="x", raw={"role": "model"}, provider="fake")
    thinking = plain.copy()
    thinking.replay_tokens = 800
    base = est.estimate("s", [], [plain])
    assert est.estimate("s", [], [thinking]) == base + 800
    # Calibration only sees the text part: reported - replay.
    raw = est.raw("s", [], [thinking])
    est.observe(raw, est.estimate("s", [], [thinking]), int(raw) + 800, replay=800)
    assert est.factor == pytest.approx(1.0, abs=0.02)


def test_preserved_thinking_share_of_prompt_and_budget():
    calls = [
        {"prompt_tokens": 1000, "budget": 12000, "view": {"replay_tokens": 0}},
        {"prompt_tokens": 4000, "budget": 12000, "view": {"replay_tokens": 1000}},
        {"prompt_tokens": 12000, "budget": 12000, "view": {"replay_tokens": 6000}},
        {"prompt_tokens": None, "budget": 12000, "view": {"replay_tokens": 5}},  # skipped
    ]
    out = accounting.preserved_thinking(calls)
    assert out["calls"] == 3 and out["calls_with_thinking"] == 2
    assert out["share_of_prompt"] == {"mean": 0.25, "median": 0.25, "max": 0.5}
    assert out["share_of_budget"]["max"] == 0.5 and out["tokens"]["max"] == 6000
    assert accounting.preserved_thinking([]) == {"calls": 0}
    env = run(agent([reply(("submit", {}))]))
    acct = accounting.summarize(env.steps, env.events, "gemma-4-26b-a4b-it")
    assert acct["preserved_thinking"]["calls"] == 1
    assert acct["preserved_thinking"]["share_of_prompt"]["max"] == 0.0


def test_a_call_held_across_the_quota_reset_keeps_the_episode_going():
    """A 20-hour daily-quota hold: same episode and sandbox, not active time, and not counted
    by the 6-hour elapsed safety cap (D16, D17)."""
    from repogym.providers.base import Attempt as _Attempt

    clock = FakeClock()
    script = [reply(("bash", {"command": "ls"})) for _ in range(3)] + [reply(("submit", {}))]
    a = agent(script)
    inner = a.provider.generate
    held = {"done": False}

    def generate(*args, **kwargs):
        gen = inner(*args, **kwargs)
        gen.attempts = [_Attempt(200, "ok")]
        gen.latency_seconds = 1.0
        if not held["done"]:  # the second call waits for the reset
            held["done"] = True
            gen.attempts = [_Attempt(None, "daily_quota_pause", None, 72_000.0), *gen.attempts]
        clock.t += gen.wait_seconds + 1.0
        return gen

    a.provider.generate = generate
    env = env_with(Budgets(max_wall_seconds=600, max_elapsed_seconds=6 * 3600))
    env._clock, env._t0 = clock, clock()
    a.run(EnvFacade(env), Observation(text="Fix the issue: X is broken."))
    state = env.state()
    assert state.done_reason == "submitted" and state.step_count == 4  # one episode throughout
    assert state.elapsed_seconds == pytest.approx(72_000 + 4.0)
    assert state.quota_pause_seconds == pytest.approx(72_000.0)
    assert state.active_seconds == pytest.approx(4.0)
