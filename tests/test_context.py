"""Context strategies: invariants as property tests over random histories, plus targeted cases."""

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from repogym.context.base import (
    MASK_TEXT,
    SUMMARY_HEADER,
    ContextConfig,
    ContextOverflow,
    check_view,
    clip_text,
    split_units,
)
from repogym.context.strategies import STRATEGIES, ContextManager
from repogym.context.tokens import TokenEstimator
from repogym.providers.base import Message, ToolCall, ToolResult

SYSTEM = "You are an agent."
TOOLS = [{"name": "bash", "description": "run", "parameters": {"type": "object"}}]


def assistant(i, n_calls=1):
    calls = [
        ToolCall(id=f"c{i}_{k}", name="bash", args={"command": f"cmd {i} {k}"})
        for k in range(n_calls)
    ]
    raw = {
        "role": "model",
        "parts": [
            {
                "function_call": {"id": c.id, "name": c.name, "args": c.args},
                **({"thought_signature": f"sig{i}"} if k == 0 else {}),
            }
            for k, c in enumerate(calls)
        ],
    }
    return Message(role="assistant", text=f"step {i}", calls=calls, raw=raw, provider="gemini")


def tool(i, sizes):
    return Message(
        role="tool",
        results=[
            ToolResult(f"c{i}_{k}", "bash", f"out{i}:" + "x" * n) for k, n in enumerate(sizes)
        ],
    )


def history_from(spec, task_chars=200):
    history = [Message(role="user", text="Fix the bug. " + "t" * task_chars)]
    for i, (sizes, nudge) in enumerate(spec, start=1):
        if nudge:  # a reply with no tool call, answered by a nudge
            history.append(
                Message(
                    role="assistant",
                    text="thinking aloud",
                    raw={"role": "model", "parts": [{"text": "thinking aloud"}]},
                    provider="gemini",
                )
            )
            history.append(Message(role="user", text="Call a tool."))
        else:
            history += [assistant(i, len(sizes)), tool(i, sizes)]
    return history


def fake_summarizer(calls):
    def summarize(previous, units):
        calls.append([u.index for u in units])
        covered = [u.index for u in units]
        return f"{previous or ''} [covered {covered}]".strip(), {"prompt_tokens": 100}

    return summarize


def manager(strategy, budget, **kw):
    calls = []
    cm = ContextManager(
        strategy,
        ContextConfig(budget=budget, **kw),
        TokenEstimator(),
        summarizer=fake_summarizer(calls) if strategy in ("llm_summary", "hybrid") else None,
    )
    return cm, calls


units_spec = st.lists(
    st.tuples(
        st.lists(st.integers(0, 12_000), min_size=1, max_size=3),
        st.booleans().map(lambda b: b and False) | st.just(False),
    ),
    min_size=1,
    max_size=25,
)
nudges = st.lists(st.booleans(), min_size=25, max_size=25)


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    spec=units_spec,
    nudge=nudges,
    strategy=st.sampled_from(STRATEGIES),
    budget=st.integers(3_000, 20_000),
)
def test_every_strategy_respects_budget_prefix_last_unit_and_pairs(spec, nudge, strategy, budget):
    # Nudge turns only between tool units, never as the latest unit.
    spec = [(sizes, nudge[i] and i < len(spec) - 1) for i, (sizes, _) in enumerate(spec)]
    history = history_from(spec)
    cm, _ = manager(strategy, budget)
    try:
        view = cm.build(SYSTEM, TOOLS, history)
    except ContextOverflow:
        # Legitimate if the pinned prefix plus the (clipped) latest unit can't fit, or, for the
        # summarising strategies, if the stored summary is what pushes the minimal view over B
        # (the episode ends as context_failure; the summary is never silently left out).
        if cm.summary:
            return
        minimal = ContextManager("sliding_window", ContextConfig(budget=10**9), TokenEstimator())
        task, units = split_units(history)
        tail = minimal.build(SYSTEM, TOOLS, [history[0], *units[-1].messages])
        assert tail.estimated_tokens > budget
        return
    assert view.estimated_tokens <= budget
    assert check_view(history, view) == []
    again = manager(strategy, budget)[0]
    again.summary, again.summary_covers = cm.summary, cm.summary_covers
    assert again.build(SYSTEM, TOOLS, history).messages == view.messages  # deterministic


def test_clip_is_head_and_tail_with_an_explicit_marker():
    est = TokenEstimator()
    text = "H" * 50_000 + "T" * 50_000
    clipped, removed = clip_text(text, 2_000, est)
    assert clipped.startswith("HHHH") and clipped.endswith("TTTT") and removed > 90_000
    assert "characters" in clipped and "clipped by RepoGym" in clipped
    assert clip_text("short", 2_000, est) == ("short", 0)


def test_clip_applies_to_every_strategy_alike():
    history = history_from([([60_000], False), ([10], False)])
    for strategy in STRATEGIES:
        view = manager(strategy, 200_000)[0].build(SYSTEM, TOOLS, history)
        first_obs = view.messages[2].results[0].output
        assert "clipped by RepoGym" in first_obs and view.units[1] == "clipped", strategy


def test_obs_mask_keeps_last_m_observations_and_never_masks_the_latest_unit():
    history = history_from([([500], False)] * 8)
    view = manager("obs_mask", 200_000, keep_observations=3)[0].build(SYSTEM, TOOLS, history)
    outputs = [m.results[0].output for m in view.messages if m.role == "tool"]
    assert [o.startswith("[observation from") for o in outputs] == [True] * 5 + [False] * 3
    assert MASK_TEXT.format(tool="bash", chars=len("out1:" + "x" * 500)) == outputs[0]
    assert view.units[1] == "masked" and view.units[8] == "full"


def test_sliding_window_drops_oldest_whole_units():
    history = history_from([([4_000], False)] * 6)
    view = manager("sliding_window", 6_000)[0].build(SYSTEM, TOOLS, history)
    kept = [i for i, s in view.units.items() if s != "dropped"]
    assert kept == list(range(kept[0], 7)) and kept[-1] == 6 and len(kept) < 6
    assert [e.kind for e in view.events] == ["drop"]


def test_llm_summary_is_appended_to_the_task_and_is_incremental():
    cm, calls = manager("llm_summary", 6_000, keep_recent_units=2)
    history = history_from([([3_000], False)] * 5)
    view = cm.build(SYSTEM, TOOLS, history)
    head = view.messages[0]
    assert head.text == history[0].text and head.extra_text[0].startswith(SUMMARY_HEADER)
    assert calls == [[1, 2, 3]] and view.units[1] == "summarized" and view.units[5] == "full"
    # Grow the history until the prompt crosses the trigger again: the next summary call
    # receives only units not yet covered (the earlier summary text carries the rest).
    i = 6
    while len(calls) == 1:
        history += [assistant(i), tool(i, [3_000])]
        cm.build(SYSTEM, TOOLS, history)
        i += 1
    assert calls[1] and min(calls[1]) == 4 and max(calls[1]) == i - 1 - 2


def test_hybrid_masks_first_and_summarises_only_if_still_over_the_trigger():
    cm, calls = manager("hybrid", 50_000, keep_observations=2, keep_recent_units=2)
    cm.build(SYSTEM, TOOLS, history_from([([3_000], False)] * 6))
    assert calls == []  # masking alone got under the trigger
    # With B=5,000 the masked prompt (~4,450 tokens; observations are clipped first) is over
    # the 4,000-token trigger, so the oldest masked units get summarised.
    cm2, calls2 = manager("hybrid", 5_000, keep_observations=2, keep_recent_units=2)
    view = cm2.build(SYSTEM, TOOLS, history_from([([6_000], False)] * 6))
    kinds = [e.kind for e in view.events]
    assert calls2 == [[1, 2, 3, 4]] and kinds.index("mask") < kinds.index("summary")


def test_overflow_when_even_the_minimal_view_cannot_fit():
    with pytest.raises(ContextOverflow):
        manager("obs_mask", 500)[0].build(SYSTEM, TOOLS, history_from([([3_000], False)]))


def test_estimator_calibrates_toward_reported_tokens():
    est = TokenEstimator()
    for _ in range(20):
        record = est.observe(raw_estimate=1000.0, estimated=1050, reported=600)
    assert 0.55 < est.factor < 0.65 and record["error"] == pytest.approx(0.75)


def test_check_view_catches_orphans_edits_and_lost_prefix():
    history = history_from([([10], False), ([10], False)])
    view = manager("sliding_window", 200_000)[0].build(SYSTEM, TOOLS, history)
    orphan = [
        view.messages[0],
        view.messages[2],
        *view.messages[3:],
    ]  # drop a call, keep its result
    view.messages = orphan
    assert any("orphaned" in p for p in check_view(history, view))
    view2 = manager("sliding_window", 200_000)[0].build(SYSTEM, TOOLS, history)
    view2.messages[1] = view2.messages[1].copy()
    view2.messages[1].raw = {"role": "model", "parts": []}
    assert any("edited" in p for p in check_view(history, view2))
    view2.messages[0] = Message(role="user", text="different task")
    assert any("verbatim" in p for p in check_view(history, view2))


def test_units_covered_by_a_summary_are_never_shown_again_after_masking():
    # Regression (found by the property test): masking ran after summarized units were marked,
    # turned them back into "masked", and they reappeared in the prompt on the next build.
    history = history_from(
        [([11], False), ([119], False), ([0], False), ([0], False), ([0, 0, 4640], False)]
    )
    cm, _ = manager("hybrid", 3000)
    first = cm.build(SYSTEM, TOOLS, history)
    again, _ = manager("hybrid", 3000)
    again.summary, again.summary_covers = cm.summary, cm.summary_covers
    second = again.build(SYSTEM, TOOLS, history)
    assert first.units[1] == second.units[1] == "summarized" and first.messages == second.messages




def test_a_summary_that_alone_makes_the_minimal_view_exceed_b_is_a_context_failure():
    # Found by hypothesis: pinned prefix + latest unit + the summary is 1 token over B = 3000.
    spec = [([743], False), ([57], True), ([1793], True), ([2852], False), ([0, 1717, 4756], False)]
    history = history_from(spec)
    cm, _ = manager("llm_summary", 3000)
    with pytest.raises(ContextOverflow):
        cm.build(SYSTEM, TOOLS, history)
