# Week 3 runbook

Operating the experiment runs: starting, watching, stopping, and recovering. Decisions behind
these rules are in DECISIONS.md (D11, D16, D17, D20).

## Process safety (rule)

* **Every long-running job is started with `scripts/jobs.py start`**, in its own session and
  process group.
  * Its PID, process group, command, log and start time are written to
    `<dir>/<name>.pid.json`.
  * For experiment jobs, `<dir>` is `data/runs/<experiment>/jobs`; for anything else it is
    `data/jobs`.
  * The runner also records itself in `data/runs/<experiment>/pids/runner-<models>.json`, and
    marks it `ended_at` on exit.
* **Jobs are stopped only by their recorded process group:**
  `python3 scripts/jobs.py stop <name> --dir <dir> [--run-dir data/runs/<experiment>]`.
  * It sends SIGINT to exactly that group, after checking that the recorded PID still runs the
    recorded command, so a reused PID is never signalled.
  * If the job is still running after the timeout, it says so and stops there. Escalating to
    SIGTERM or SIGKILL is a deliberate manual step, again by recorded process group:
    `kill -TERM -<pgid>`.
* **Never use `pkill -f`, `killall`, or `pgrep … | xargs kill`**, or any other name or pattern
  match.
  * A pattern also matches the shell that runs it, and any unrelated process whose command
    line happens to contain the text.
  * On 2026-11-09 a `pkill -f scripts/run_when_quota.py` killed the very shell issuing it. No
    run was affected, but it could as easily have hit one.
* **A job started before this rule** is recorded with
  `python3 scripts/jobs.py adopt <name> --dir <dir> --pid <pid>`, after checking that its
  process group is its own (`ps -o pgid= -p <pid>`).
* **To see what's running:** `python3 scripts/jobs.py status --dir <dir>`.

## Containers

* Each episode runs in a container named `repogym-ep-<random>`, running `sleep infinity`. It
  lives until the episode closes it, or until Docker restarts.
* The runner records the running episode's container in its heartbeat
  (`data/runs/<experiment>/heartbeat/<model>.json`, field `container`).
* `jobs.py stop … --run-dir data/runs/<experiment>` removes exactly the containers that the
  heartbeats list as running. It never removes containers by a name pattern.
* **One gap:** if a job is stopped during sandbox setup, before the heartbeat has the name, the
  container may be left over. Check `docker ps --format '{{.Names}} {{.CreatedAt}}'` and
  remove a leftover by its exact name, only after matching its creation time to the
  interrupted episode.

## Starting runs

```bash
# Now:
python3 scripts/jobs.py start gemma --dir data/runs/week3-repilot/jobs -- \
    ~/.local/bin/uv run repogym run --experiment configs/experiments/repilot.yaml \
    --models gemma-4-26b-a4b-it
# At a time (UTC), e.g. after a quota reset:
python3 scripts/jobs.py start flash --dir data/runs/week3-repilot/jobs \
    --at 2026-11-10T08:05:00Z -- ~/.local/bin/uv run repogym run \
    --experiment configs/experiments/repilot.yaml --models gemini-3.5-flash-lite
```

* The tree must be committed before a run starts: provenance records the commit and whether
  the tree is dirty.
* Test-split runs need the frozen config and `--final-test-run` (the guard).

## Watching runs

* `uv run repogym status --experiment <file>`: per model, cells done, solves, attempts, quota
  used today, tokens, budget violations, `summary_omitted`, `missing_reasoning`. For a running
  episode it also shows the cell, attempt, step, minutes in, and seconds since the last model
  call.
* `python3 scripts/reliability_by_hour.py`: server-error rate per model by UTC hour, from
  `data/quota/attempts-<day>.jsonl` (every API attempt).
* `python3 scripts/pilot_quota_analysis.py <run-dir>`: input tokens per minute against TPM.

## Quota and retries (what happens automatically)

* **Per-minute limits:** the limiter keeps each model under 90% of its RPM and TPM.
  `data/quota/ledger.json` counts every attempt per model per Pacific day; it is the source of
  truth for our own request counts (D20).
* **Daily quota:** quotas reset at **midnight Pacific**, which is 08:00 UTC in winter and 07:00
  UTC in summer. If a model's daily quota runs out mid-episode, the call is **held in place**
  until the reset plus 60 s, and the episode continues with the same sandbox. The hold isn't
  active time and isn't counted by the 6-hour elapsed cap (D16).
* **Server errors (5xx, transport):** retried in place with backoff from 30 s, capped at 10 min
  between tries. A call fails only after 20 consecutive errors or 60 minutes (D16). A cell gets
  at most 3 `api_failure` attempts, and every attempt keeps its own files.

## After a stop or crash

* Rerun the same `repogym run --experiment …` command. Finished cells are skipped. An episode
  that was interrupted left no record and reruns as the next attempt.
* Pilot runs are tagged `pilot` and excluded from analysis, whatever happens to them.

## Scheduled now (2026-11-09)

| Job | Where it's recorded | When |
|---|---|---|
| Gemma 26B re-pilot | `data/runs/week3-repilot/jobs/gemma.pid.json` | running |
| 3.5 Flash-Lite re-pilot | `data/runs/week3-repilot/jobs/flash-0805.pid.json` | 2026-11-10 08:05 UTC |
| 3.1 Flash-Lite pilot | `data/runs/week3-pilot-gemini31/jobs/gemini31.pid.json` | 2026-11-10 08:05 UTC |
| 3.5 Flash-Lite `high` check | `data/runs/week3-flash-thinking-high/jobs/scheduler.pid.json` | after the 3.5 re-pilot |
| Gemma 31B reliability probe | `data/jobs/probe-31b.pid.json` | until about 00:30 UTC on 2026-11-10 |
