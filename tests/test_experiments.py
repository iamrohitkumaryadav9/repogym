"""Experiment files, the test-split guard and the runner (fake episodes; no Docker, no API)."""

import json
import threading
from pathlib import Path

import pytest
import yaml

from repogym.env.budgets import Budgets
from repogym.experiments import guard
from repogym.experiments.runner import Runner, cell_done, latest_records
from repogym.experiments.spec import ExperimentError, load, max_clip_chars, sha256_bytes
from repogym.experiments.status import summary
from repogym.harness import EpisodeRecord, _finish
from repogym.ratelimit import LimitsConfig, QuotaLedger

DEV = ["python-attrs__attrs-1448", "pallets__click-3504"]
TEST = ["python-attrs__attrs-1457"]


def write_exp(tmp_path, **over):
    data = {
        "name": "exp-test",
        "kind": "pilot",
        "split": "dev",
        "tasks": DEV,
        "models": ["gemma-4-26b-a4b-it", "gemini-3.5-flash-lite"],
        "strategies": ["obs_mask"],
        "repeats": 1,
        "context": {"budget": 12000},
        "budgets": {"max_steps": 75, "max_observation_chars": 200_000},
    }
    data.update(over)
    path = tmp_path / f"{data['name']}.yaml"
    path.write_text(yaml.safe_dump(data))
    exp = load(path)
    exp.runs_root = tmp_path / "runs"
    return exp


# -- experiment files ------------------------------------------------------------------------


def test_cells_keys_and_hashes(tmp_path):
    exp = write_exp(tmp_path, repeats=2, strategies=["obs_mask", "hybrid"])
    cells = exp.cells()
    assert len(cells) == 2 * 2 * 2 * 2 and len({c.key for c in cells}) == len(cells)
    assert cells[0].key == "gemma-4-26b-a4b-it__obs_mask__python-attrs__attrs-1448__r0"
    assert exp.file_sha256 == sha256_bytes((tmp_path / "exp-test.yaml").read_bytes())
    again = write_exp(tmp_path, repeats=2, strategies=["obs_mask", "hybrid"])
    assert again.resolved_sha256 == exp.resolved_sha256
    changed = write_exp(
        tmp_path, repeats=2, strategies=["obs_mask", "hybrid"], context={"budget": 11000}
    )
    assert changed.resolved_sha256 != exp.resolved_sha256
    assert exp.resolved()["prompts"]["system"] and exp.resolved()["tools"]


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({"strategies": ["raw_unbounded"]}, "unknown strategies"),
        ({"tasks": ["nope__x-1"]}, "unknown task ids"),
        ({"tasks": TEST}, "not in split"),
        ({"models": ["gpt-x"]}, "not in models.yaml"),
        ({"kind": "final"}, "kind must be"),
    ],
)
def test_invalid_experiments_are_rejected(tmp_path, over, error):
    with pytest.raises(ExperimentError, match=error):
        write_exp(tmp_path, **over)


def test_stored_observation_cap_must_exceed_the_prompt_clip(tmp_path):
    """D14: the environment may never cut what the clip would have shown the model."""
    with pytest.raises(ExperimentError, match="must exceed the largest prompt clip"):
        write_exp(tmp_path, budgets={"max_steps": 75})  # Week 2 default: 10,000 chars
    exp = write_exp(tmp_path)
    assert exp.budgets.max_observation_chars > max_clip_chars(exp.context)
    for config in sorted((Path(__file__).parents[1] / "configs" / "experiments").glob("*.yaml")):
        assert load(config).budgets.max_observation_chars > max_clip_chars(load(config).context)


# -- the guard -------------------------------------------------------------------------------


@pytest.fixture
def frozen(tmp_path):
    """A frozen config matching a test-split experiment, recorded in a pre-registration."""

    def make(**over):
        exp = write_exp(tmp_path, kind="e3", split="test", tasks=TEST, name="e3", **over)
        values = {k: exp.resolved()[k] for k in guard.FROZEN_KEYS}
        path = tmp_path / "frozen.yaml"
        path.write_text(yaml.safe_dump(values))
        digest = sha256_bytes(path.read_bytes())
        prereg = tmp_path / "PREREGISTRATION.md"
        prereg.write_text(f"Frozen config sha256: {digest}\n")
        exp.frozen_config = {"path": str(path), "sha256": digest}
        return exp, prereg, path, digest

    return make


def check(exp, prereg, tmp_path, final=True, clean=True):
    guard.check(
        exp,
        final,
        prereg=prereg,
        refusals=tmp_path / "refusals.jsonl",
        clean=lambda: clean,
        root=tmp_path,
    )


def test_pilot_may_not_touch_test_tasks_and_the_refusal_is_logged(tmp_path):
    exp = write_exp(tmp_path)
    exp.split, exp.task_ids = "test", TEST
    with pytest.raises(guard.RunRefused, match="dev tasks only"):
        check(exp, tmp_path / "none.md", tmp_path, final=False)
    log = [json.loads(line) for line in (tmp_path / "refusals.jsonl").read_text().splitlines()]
    assert log[0]["kind"] == "pilot" and "dev tasks only" in log[0]["reason"]


def test_dev_runs_pass_and_final_flag_on_dev_is_refused(tmp_path):
    exp = write_exp(tmp_path, kind="e1")
    check(exp, tmp_path / "none.md", tmp_path, final=False)
    with pytest.raises(guard.RunRefused, match="touches no test task"):
        check(exp, tmp_path / "none.md", tmp_path, final=True)


def test_test_split_run_passes_only_with_everything_in_place(tmp_path, frozen):
    exp, prereg, _, _ = frozen()
    check(exp, prereg, tmp_path)  # all conditions hold
    for kwargs, reason in [
        ({"final": False}, "without --final-test-run"),
        ({"clean": False}, "not clean"),
    ]:
        with pytest.raises(guard.RunRefused, match=reason):
            check(exp, prereg, tmp_path, **kwargs)


def test_test_split_refusals(tmp_path, frozen):
    exp, prereg, path, digest = frozen()
    no_frozen = write_exp(tmp_path, kind="e3", split="test", tasks=TEST, name="e3b")
    with pytest.raises(guard.RunRefused, match="without a frozen_config"):
        check(no_frozen, prereg, tmp_path)
    exp.frozen_config = {"path": str(path), "sha256": "0" * 64}
    with pytest.raises(guard.RunRefused, match="hash mismatch"):
        check(exp, prereg, tmp_path)
    exp.frozen_config = {"path": str(path), "sha256": digest}
    prereg.write_text("no hash here")
    with pytest.raises(guard.RunRefused, match="not recorded in PREREGISTRATION"):
        check(exp, prereg, tmp_path)
    prereg.write_text(digest)
    exp.context.budget = 9999  # differs from the frozen value
    with pytest.raises(guard.RunRefused, match="differs from the frozen config"):
        check(exp, prereg, tmp_path)
    refusals = (tmp_path / "refusals.jsonl").read_text().splitlines()
    assert len(refusals) == 4


# -- the runner ------------------------------------------------------------------------------


def fake_record(task, agent, run_dir, budgets, **kw):
    return EpisodeRecord(
        run_id=run_dir.name,
        episode_id="e",
        task_id=task.task_id,
        agent="a",
        agent_version="1",
        status="graded",
        reward=1,
        done_reason="submitted",
        started_at="2026-11-09T00:00:00Z",
        ended_at="2026-11-09T00:10:00Z",
        release="v0.1",
        tasks_sha256="s",
        image="i",
        image_digest="d",
        repogym_commit="c",
        repogym_dirty=False,
        budgets=budgets,
        episode_key=kw["episode_key"],
        experiment=kw["experiment"],
        attempt=kw.get("attempt"),
    )


class FakeEpisodes:
    def __init__(self, outcomes=None):
        self.calls, self.outcomes, self.lock = [], dict(outcomes or {}), threading.Lock()

    def __call__(self, task, agent, run_dir, budgets, **kw):
        record = fake_record(task, agent, run_dir, budgets, **kw)
        with self.lock:
            self.calls.append(kw["episode_key"])
            queue = self.outcomes.get(kw["episode_key"], [])
            if queue:
                for field, value in queue.pop(0).items():
                    setattr(record, field, value)
        return _finish(record, run_dir)


def runner(exp, episodes, tmp_path, clock=None, sleeps=None):
    sleeps = [] if sleeps is None else sleeps
    return Runner(
        exp,
        "unused",
        episode_fn=episodes,
        provider_fn=lambda *a: type("P", (), {"name": a[0], "count_tokens": lambda *x: None})(),
        ledger=QuotaLedger(tmp_path / "ledger.json"),
        sleep=sleeps.append,
        clock=clock or (lambda: 1_800_000_000.0),
        guard_fn=lambda e, f: guard.check(
            e, f, prereg=tmp_path / "x.md", refusals=tmp_path / "ref.jsonl", root=tmp_path
        ),
    )


def test_every_cell_runs_once_in_parallel_and_a_rerun_does_nothing(tmp_path):
    exp = write_exp(tmp_path)
    episodes = FakeEpisodes()
    runner(exp, episodes, tmp_path).run()
    assert sorted(episodes.calls) == sorted(c.key for c in exp.cells())
    runner(exp, episodes, tmp_path).run()  # resume: everything done, nothing reruns
    assert len(episodes.calls) == len(exp.cells())
    records = [json.loads(x) for x in (exp.run_dir / "episodes.jsonl").read_text().splitlines()]
    assert all(r["experiment"]["pilot"] is True and r["experiment"]["cell"] for r in records)
    assert records[0]["experiment"]["experiment_sha256"] == exp.file_sha256


def test_harness_error_is_rerun_exactly_once(tmp_path):
    exp = write_exp(tmp_path, models=["gemma-4-26b-a4b-it"], tasks=DEV[:1])
    key = exp.cells()[0].key
    episodes = FakeEpisodes(
        {
            key: [
                {"status": "agent_error", "error_type": "KeyError"},
                {"status": "agent_error", "error_type": "KeyError"},
            ]
        }
    )
    runner(exp, episodes, tmp_path).run()
    assert episodes.calls == [key, key]  # original + one rerun, then it's done (and reported)
    assert [r["termination"] for r in latest_records(exp.run_dir)[key]] == ["harness_error"] * 2


def test_daily_quota_sleeps_until_the_pacific_reset_then_retries(tmp_path):
    exp = write_exp(tmp_path, models=["gemini-3.5-flash-lite"], tasks=DEV[:1])
    key = exp.cells()[0].key
    episodes = FakeEpisodes({key: [{"status": "agent_error", "error_type": "DailyQuotaExhausted"}]})
    sleeps, now = [], 1_800_000_000.0
    runner(exp, episodes, tmp_path, clock=lambda: now, sleeps=sleeps).run()
    reset = QuotaLedger(tmp_path / "l.json").next_reset(now)
    assert episodes.calls == [key, key] and sleeps == [pytest.approx(reset - now + 60)]
    assert latest_records(exp.run_dir)[key][-1]["termination"] == "submitted"


def test_cell_done_rules():
    assert not cell_done([])
    assert not cell_done([{"termination": "api_failure"}])
    assert cell_done([{"termination": "step_cap"}]) and cell_done(
        [{"termination": "context_failure"}]
    )
    assert not cell_done([{"termination": "harness_error"}])
    assert cell_done([{"termination": "harness_error"}, {"termination": "harness_error"}])


def test_status_reports_progress_quota_and_eta(tmp_path):
    exp = write_exp(tmp_path)
    runner(exp, FakeEpisodes(), tmp_path).run()
    ledger = QuotaLedger(tmp_path / "ledger.json")
    ledger.add("gemma-4-26b-a4b-it", 1_800_000_000.0, 7)
    rows = {r["model"]: r for r in summary(exp, now=1_800_000_000.0, ledger=ledger)}
    gemma = rows["gemma-4-26b-a4b-it"]
    assert gemma["done"] == gemma["total"] == 2 and gemma["solved"] == 2
    assert gemma["requests_today"] == 7 and gemma["rpd_limit"] == 12960
    assert gemma["mean_episode_minutes"] == 10.0 and gemma["eta_hours"] == 0.0
    assert rows["gemini-3.5-flash-lite"]["rpd_limit"] == 450


def test_budgets_from_the_experiment_file_reach_the_episode(tmp_path):
    seen = []

    def episodes(task, agent, run_dir, budgets, **kw):
        seen.append((budgets, agent.context.config.budget, agent.context.strategy))
        return _finish(fake_record(task, agent, run_dir, budgets, **kw), run_dir)

    exp = write_exp(
        tmp_path,
        models=["gemma-4-26b-a4b-it"],
        tasks=DEV[:1],
        budgets={"max_steps": 75, "max_observation_chars": 200000},
        context={"budget": 11000, "keep_observations": 3},
    )
    runner(exp, episodes, tmp_path).run()
    budgets, b, strategy = seen[0]
    assert isinstance(budgets, Budgets) and budgets.max_steps == 75
    assert budgets.max_observation_chars == 200000 and b == 11000 and strategy == "obs_mask"


def test_api_failures_get_three_attempts_each_kept_and_numbered(tmp_path):
    exp = write_exp(tmp_path, models=["gemma-4-31b-it"], tasks=DEV[:1])
    key = exp.cells()[0].key
    failed = {"status": "agent_error", "error_type": "ProviderUnavailable", "reward": 0}
    episodes = FakeEpisodes({key: [failed, failed, failed]})
    sleeps = []
    runner(exp, episodes, tmp_path, sleeps=sleeps).run()
    assert episodes.calls == [key, key, key]  # three attempts, then given up and reported
    assert sleeps == [60, 60]  # no pause after giving up
    records = latest_records(exp.run_dir)[key]
    assert [r["attempt"] for r in records] == [1, 2, 3]
    assert [r["termination"] for r in records] == ["api_failure"] * 3 and cell_done(records)
    previous = records[2]["experiment"]["previous_attempts"]
    assert records[2]["experiment"]["attempt"] == 3
    assert [p["attempt"] for p in previous] == [1, 2]
    assert all(p["error_type"] == "ProviderUnavailable" for p in previous)


def test_daily_quota_interruptions_do_not_count_toward_the_three(tmp_path):
    daily = {"termination": "api_failure", "error_type": "DailyQuotaExhausted"}
    other = {"termination": "api_failure", "error_type": "ProviderUnavailable"}
    assert not cell_done([daily, daily, daily, other])
    assert not cell_done([other, other])
    assert cell_done([other, daily, other, other])


def test_elapsed_cap_waits_for_the_reset_and_counts_as_an_api_failure(tmp_path):
    exp = write_exp(tmp_path, models=["gemma-4-26b-a4b-it"], tasks=DEV[:1])
    key = exp.cells()[0].key
    episodes = FakeEpisodes({key: [{"done_reason": "budget:elapsed", "reward": 0}]})
    sleeps, now = [], 1_800_000_000.0
    runner(exp, episodes, tmp_path, clock=lambda: now, sleeps=sleeps).run()
    reset = QuotaLedger(tmp_path / "l.json").next_reset(now)
    assert episodes.calls == [key, key] and sleeps == [pytest.approx(reset - now + 60)]
    records = latest_records(exp.run_dir)[key]
    assert [r["termination"] for r in records] == ["api_failure", "submitted"]


def test_heartbeat_tracks_the_worker_and_status_shows_it(tmp_path):
    import json as _json

    from repogym.experiments.runner import Heartbeat

    exp = write_exp(tmp_path, models=["gemma-4-26b-a4b-it"], tasks=DEV[:1])
    t = [1_800_000_000.0]
    beat = Heartbeat(exp.run_dir, "gemma-4-26b-a4b-it", clock=lambda: t[0])
    beat.start("cell-1", 2)
    t[0] += 30
    beat.progress(7)
    t[0] += 45
    row = summary(exp, now=t[0], ledger=QuotaLedger(tmp_path / "ledger.json"))[0]
    w = row["worker"]
    assert w["state"] == "running" and w["cell"] == "cell-1" and w["attempt"] == 2
    assert w["step"] == 7 and w["seconds_since_model_call"] == 45 and w["episode_seconds"] == 75
    beat.sleeping(t[0] + 600, "ProviderUnavailable")
    w = summary(exp, now=t[0] + 100, ledger=QuotaLedger(tmp_path / "ledger.json"))[0]["worker"]
    assert w["state"] == "sleeping" and w["sleep_remaining_seconds"] == 500
    beat.finished()
    stored = _json.loads((exp.run_dir / "heartbeat" / "gemma-4-26b-a4b-it.json").read_text())
    assert stored["state"] == "done"


def test_a_full_run_leaves_a_done_heartbeat_per_model(tmp_path):
    exp = write_exp(tmp_path)
    runner(exp, FakeEpisodes(), tmp_path).run()
    rows = summary(exp, now=1_800_000_000.0, ledger=QuotaLedger(tmp_path / "ledger.json"))
    assert all(r["worker"]["state"] == "done" for r in rows)
    assert all(r["attempts"] == 2 and r["cells_retried"] == 0 for r in rows)


def test_episode_files_are_never_overwritten(tmp_path):
    from repogym.harness import file_stem, unique_stem

    assert file_stem("cell", None) == "cell" and file_stem("cell", 2) == "cell__a2"
    first = unique_stem(tmp_path, "cell__a1")
    (tmp_path / "trajectories" / f"{first}.json").write_text("{}")
    second = unique_stem(tmp_path, "cell__a1")  # the same name again: suffixed, not reused
    assert first == "cell__a1" and second == "cell__a1__dup2"
    (tmp_path / "diffs").mkdir()
    (tmp_path / "diffs" / "other.diff").write_text("")
    assert unique_stem(tmp_path, "other") == "other__dup2"


def test_the_committed_pilot_is_dev_only_and_the_guard_refuses_any_test_task(tmp_path):
    pilot = load(Path(__file__).parents[1] / "configs" / "experiments" / "pilot.yaml")
    assert pilot.kind == "pilot" and pilot.split == "dev" and len(pilot.cells()) == 9
    assert pilot.budgets.max_elapsed_seconds == 6 * 3600 and pilot.budgets.max_steps == 75
    check(pilot, tmp_path / "none.md", tmp_path, final=False)  # allowed as is
    pilot.task_ids = [*pilot.task_ids, TEST[0]]
    with pytest.raises(guard.RunRefused, match="dev tasks only"):
        check(pilot, tmp_path / "none.md", tmp_path, final=False)
    with pytest.raises(guard.RunRefused):
        check(pilot, tmp_path / "none.md", tmp_path, final=True)


def test_a_run_can_be_limited_to_some_models(tmp_path):
    exp = write_exp(tmp_path)
    episodes = FakeEpisodes()
    runner(exp, episodes, tmp_path).run(["gemma-4-26b-a4b-it"])
    assert {k.split("__")[0] for k in episodes.calls} == {"gemma-4-26b-a4b-it"}
    runner(exp, episodes, tmp_path).run()  # the rest later, same experiment
    assert sorted(episodes.calls) == sorted(c.key for c in exp.cells())
    with pytest.raises(ValueError, match="not in the experiment"):
        runner(exp, episodes, tmp_path).run(["gpt-x"])


def test_the_repilot_is_dev_only_on_new_tasks(tmp_path):
    root = Path(__file__).parents[1] / "configs" / "experiments"
    first, second = load(root / "pilot.yaml"), load(root / "repilot.yaml")
    assert second.kind == "pilot" and second.split == "dev" and len(second.cells()) == 6
    assert not set(first.task_ids) & set(second.task_ids)
    check(second, tmp_path / "none.md", tmp_path, final=False)


def test_retry_thresholds_are_preregistered_hashed_and_overridable(tmp_path):
    exp = write_exp(tmp_path)
    r = exp.resolved()["retry"]
    assert r["server_base_delay"] == 30.0 and r["server_max_delay"] == 600.0
    assert r["server_max_failures"] == 20 and r["server_max_seconds"] == 3600.0
    assert r["pause_on_daily"] is True and "retry" in guard.FROZEN_KEYS
    changed = write_exp(tmp_path, retry={"server_max_seconds": 1800})
    assert changed.retry.server_max_seconds == 1800 and changed.retry.pause_on_daily is True
    assert changed.resolved_sha256 != exp.resolved_sha256


def test_the_gemini31_pilot_matches_the_repilot_tasks_at_high_thinking(tmp_path):
    root = Path(__file__).parents[1] / "configs" / "experiments"
    g31, repilot = load(root / "pilot_gemini31.yaml"), load(root / "repilot.yaml")
    assert g31.kind == "pilot" and g31.models == ["gemini-3.1-flash-lite"]
    assert g31.task_ids == repilot.task_ids
    assert g31.model_settings["gemini-3.1-flash-lite"]["thinking_level"] == "high"
    check(g31, tmp_path / "none.md", tmp_path, final=False)
    limits = LimitsConfig.load(g31.limits_path).effective("gemini-3.1-flash-lite")
    assert limits == {"rpm": 13, "tpm": 225000, "rpd": 450}


def test_records_being_written_by_another_process_are_skipped_not_fatal(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    done = json.dumps({"episode_key": "a", "termination": "submitted"})
    (run_dir / "episodes.jsonl").write_text(done + "\n" + done[:40])  # second line half written
    assert list(latest_records(run_dir)) == ["a"]
    (run_dir / "episodes.jsonl").write_text(done[:40] + "\n" + done + "\n")
    with pytest.raises(ValueError):  # a malformed COMPLETE line is real corruption
        latest_records(run_dir)
