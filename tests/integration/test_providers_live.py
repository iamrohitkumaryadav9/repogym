"""Live, manual integration tests of tool calling against the Gemini API free tier (D8–D12).

They spend a few requests per model from the shared daily quota (counted in the ledger), so
they never run in CI: ``pytest -m live tests/integration/test_providers_live.py``. Each one
checks a behaviour the context manager relies on and that the docs can't settle alone:

* multi-turn function calling with thought signatures replayed verbatim (D12);
* a history with a middle step dropped and a summary added to the task message is accepted
  (what sliding_window, obs_mask fallback, llm_summary and hybrid send).

(A countTokens agreement test was archived with countTokens itself; see docs/week3/ARCHIVE.md.)
"""

import pytest
import yaml

from repogym.config import ROOT
from repogym.experiments.runner import LEDGER_PATH, gemini_api_key, make_provider
from repogym.providers.base import Message, ToolResult
from repogym.ratelimit import LimitsConfig, QuotaLedger, RateLimiter
from repogym.tools.definitions import TOOLS

pytestmark = pytest.mark.live
MODELS = yaml.safe_load((ROOT / "configs" / "models.yaml").read_text())["models"]
SYSTEM = (
    "You are testing a tool interface. Always act by calling exactly one tool per reply. "
    "Never answer in plain text."
)
TASK = (
    "Step 1: call bash with command `echo alpha`. Step 2: after seeing its output, call bash "
    "with command `echo beta`. Step 3: after seeing that, call submit."
)


@pytest.fixture(scope="module", params=list(MODELS))
def provider(request):
    limits = LimitsConfig.load(ROOT / "configs" / "limits" / "gemini_free_tier.yaml")
    ledger = QuotaLedger(LEDGER_PATH, limits.reset_timezone)
    limiter = RateLimiter(request.param, limits, ledger)
    return make_provider(request.param, MODELS[request.param], gemini_api_key(), limiter)


def signatures(message: Message) -> list[bool]:
    return [
        "thought_signature" in part or "thoughtSignature" in part
        for part in (message.raw or {}).get("parts", [])
        if "function_call" in part or "functionCall" in part
    ]


def answer(history: list[Message], reply: Message, output: str) -> None:
    history.append(reply)
    history.append(
        Message(
            role="tool",
            results=[ToolResult(call_id=c.id, name=c.name, output=output) for c in reply.calls],
        )
    )


def step(provider, history):
    gen = provider.generate(SYSTEM, history, TOOLS, estimated_tokens=2000)
    assert gen.message.calls, f"{provider.name}: no function call ({gen.message.text!r})"
    return gen


def test_multi_turn_tool_calling_replays_signatures(provider):
    history = [Message(role="user", text=TASK)]
    first = step(provider, history)
    assert first.message.calls[0].name == "bash"
    print(provider.name, "signatures on first reply:", signatures(first.message))
    answer(history, first.message, "alpha\n")
    second = step(provider, history)  # the first call's signature is validated here
    answer(history, second.message, "beta\n")
    third = step(provider, history)
    assert all(g.http_status == 200 for g in (first, second, third))
    names = [g.message.calls[0].name for g in (first, second, third)]
    print(provider.name, "calls:", names, "usage:", third.usage)


def test_dropped_middle_step_and_summary_part_are_accepted(provider):
    history = [Message(role="user", text=TASK)]
    first = step(provider, history)
    answer(history, first.message, "alpha\n")
    second = step(provider, history)
    answer(history, second.message, "beta\n")
    # What a context strategy sends: the task message plus a summary part, with the first step
    # dropped. The remaining assistant message is replayed verbatim with its signature.
    task = history[0].copy()
    task.extra_text = ["Summary of earlier steps: bash `echo alpha` printed alpha."]
    view = [task, *history[3:]]
    third = step(provider, view)
    assert third.http_status == 200
    print(provider.name, "after drop+summary:", third.message.calls[0].name)
