"""The limiter and ledger with a fake clock: limits never exceeded, ledger persists, reset is
midnight Pacific (DST-correct), and backoff is bounded."""

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from repogym.providers.base import DailyQuotaExhausted, ProviderUnavailable
from repogym.providers.retry import Outcome, RetryPolicy, run_with_retries
from repogym.ratelimit import LimitsConfig, ModelLimits, QuotaLedger, RateLimiter

ROOT = Path(__file__).resolve().parents[1]


class FakeClock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.t += seconds


def config(rpm=10, tpm=1000, rpd=50, margin=1.0):
    return LimitsConfig(
        models={"m": ModelLimits(rpm, tpm, rpd)},
        margin=margin,
        reset_timezone="America/Los_Angeles",
        source="test",
        verified="today",
    )


def limiter(tmp_path, clock, **kw):
    return RateLimiter(
        "m", config(**kw), QuotaLedger(tmp_path / "ledger.json"), clock=clock, sleep=clock.sleep
    )


def test_committed_limits_file_loads_with_source_and_margin():
    cfg = LimitsConfig.load(ROOT / "configs/limits/gemini_free_tier.yaml")
    assert cfg.margin == 0.9 and cfg.source and cfg.verified == "2026-11-09"
    assert cfg.effective("gemma-4-26b-a4b-it") == {"rpm": 27, "tpm": 14400, "rpd": 12960}
    assert cfg.effective("gemini-3.5-flash-lite") == {"rpm": 13, "tpm": 225000, "rpd": 450}


@settings(max_examples=60, deadline=None)
@given(
    st.lists(st.integers(min_value=0, max_value=600), min_size=1, max_size=60),
    st.lists(st.floats(min_value=0, max_value=20), min_size=60, max_size=60),
)
def test_rolling_minute_limits_are_never_exceeded(tokens, gaps):
    import tempfile

    clock = FakeClock()
    with tempfile.TemporaryDirectory() as tmp:
        lim = RateLimiter(
            "m",
            config(rpm=5, tpm=1000, rpd=10_000),
            QuotaLedger(Path(tmp) / "l.json"),
            clock=clock,
            sleep=clock.sleep,
        )
        sent = []
        for i, n in enumerate(tokens):
            clock.t += gaps[i]
            lim.acquire(n)
            sent.append((clock.t, n))
            window = [(t, k) for t, k in sent if clock.t - t < 60]
            assert len(window) <= 5
            assert len(window) == 1 or sum(k for _, k in window) <= 1000


def test_settle_replaces_the_estimate_with_reported_tokens(tmp_path):
    clock = FakeClock()
    lim = limiter(tmp_path, clock, rpm=100, tpm=1000)
    lim.acquire(900)
    lim.settle(200)  # the estimate was too high
    lim.acquire(700)  # fits now: 200 + 700 <= 1000, no sleep
    assert clock.sleeps == [] and lim.window_usage()["tokens"] == 900


def test_daily_quota_from_the_ledger_and_its_persistence(tmp_path):
    clock = FakeClock()
    lim = limiter(tmp_path, clock, rpm=100, tpm=10**6, rpd=3)
    for _ in range(3):
        lim.acquire(1)
    with pytest.raises(DailyQuotaExhausted) as info:
        lim.acquire(1)
    assert info.value.reset_at > clock.t
    # A new process (new limiter, same ledger file) sees the same count.
    again = limiter(tmp_path, clock, rpm=100, tpm=10**6, rpd=3)
    with pytest.raises(DailyQuotaExhausted):
        again.acquire(1)
    clock.t = info.value.reset_at + 1  # after the reset the quota is back
    again.acquire(1)


def test_a_daily_quota_429_marks_the_day_exhausted(tmp_path):
    clock = FakeClock()
    lim = limiter(tmp_path, clock, rpd=100)
    reset = lim.mark_daily_exhausted()
    with pytest.raises(DailyQuotaExhausted):
        lim.acquire(1)
    clock.t = reset + 1
    lim.acquire(1)


@pytest.mark.parametrize(
    ("utc", "expected_day", "reset_utc"),
    [
        ("2026-11-09T07:59:00", "2026-11-08", "2026-11-09T08:00:00"),  # PST: midnight = 08:00 UTC
        ("2026-11-09T08:00:00", "2026-11-09", "2026-11-10T08:00:00"),
        ("2026-07-01T06:59:00", "2026-06-30", "2026-07-01T07:00:00"),  # PDT: midnight = 07:00 UTC
        ("2026-03-08T07:30:00", "2026-03-07", "2026-03-08T08:00:00"),  # day of DST start
    ],
)
def test_reset_boundary_is_midnight_pacific(tmp_path, utc, expected_day, reset_utc):
    ledger = QuotaLedger(tmp_path / "l.json")
    ts = datetime.fromisoformat(utc).replace(tzinfo=UTC).timestamp()
    assert ledger.day(ts) == expected_day
    assert datetime.fromtimestamp(ledger.next_reset(ts), UTC).isoformat()[:19] == reset_utc


def test_margin_is_applied(tmp_path):
    clock = FakeClock()
    lim = RateLimiter(
        "m",
        config(rpm=10, margin=0.5),
        QuotaLedger(tmp_path / "l.json"),
        clock=clock,
        sleep=clock.sleep,
    )
    for _ in range(6):
        lim.acquire(1)
    assert len(clock.sleeps) == 1  # the 6th request waited: 5 = 10 x 0.5 per minute


def test_retry_backoff_is_bounded_and_every_attempt_is_recorded():
    sleeps = []
    policy = RetryPolicy(server_max_failures=5, server_base_delay=2.0, server_max_delay=10.0)
    with pytest.raises(ProviderUnavailable) as info:
        run_with_retries(
            lambda: Outcome(False, 503),
            policy=policy,
            model="m",
            sleep=sleeps.append,
            rng=random.Random(0),
        )
    assert len(info.value.attempts) == 5 and len(sleeps) == 4
    assert all(s <= 10.0 for s in sleeps)  # capped after jitter


def test_server_retry_delay_is_honoured_above_the_backoff():
    sleeps, calls = [], []
    body = {"error": {"details": [{"retryDelay": "33s"}]}}

    def send():
        calls.append(1)
        return Outcome(True, 200, "ok") if len(calls) == 2 else Outcome(False, 429, body=body)

    value, attempts = run_with_retries(send, policy=RetryPolicy(), model="m", sleep=sleeps.append)
    assert value == "ok" and sleeps[0] >= 33 and attempts[0].retry_delay == 33.0


def test_acquire_returns_the_seconds_it_waited(tmp_path):
    clock = FakeClock()
    lim = limiter(tmp_path, clock, rpm=1)
    assert lim.acquire(10) == 0.0
    waited = lim.acquire(10)  # the second request waits for the window to roll over
    assert waited == pytest.approx(60.0) and lim.waited_seconds == pytest.approx(60.0)


def test_ledger_counts_are_not_lost_across_processes(tmp_path):
    """Several runner processes share one ledger (D20): concurrent adds must all count."""
    import multiprocessing as mp

    path = tmp_path / "ledger.json"
    QuotaLedger(path).add("other-model", 1_800_000_000.0, 5)  # written earlier by someone else
    ctx = mp.get_context("fork")
    procs = [ctx.Process(target=_add_many, args=(path, f"m{i}", 50)) for i in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    ledger = QuotaLedger(path)
    assert [ledger.requests(f"m{i}", 1_800_000_000.0) for i in range(4)] == [50] * 4
    assert ledger.requests("other-model", 1_800_000_000.0) == 5


def _add_many(path, model, n):
    ledger = QuotaLedger(path)
    for _ in range(n):
        ledger.add(model, 1_800_000_000.0)
