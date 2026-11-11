# Week 3 archive (cleanup 2026-11-11)

After Gate 1, the Week 3 scope was reset to the original prompt (sections 0–6). Everything added
after that prompt was **archived, not deleted**: removed from the `week3` branch in commits named
`archive: …`, and kept in full at:

* tag **`week3-archive-2026-11-11`** (commit `299510f`)
* branch **`archive/week3-post-gate1`** (same commit)

Week 1–2 code, data and configs are as at `week2-complete`, apart from additive changes the
original prompt required (termination and provenance fields, event steps in trajectories, the log
redaction filter, new CLI commands, new dependencies).

## What was archived, and why

| Item | Why it is beyond the original prompt |
|---|---|
| Required reasoning text, `missing_reasoning` events and reminders, the per-strategy view text, `[step k/N]`, the final-steps nudge (D19) | Gate 1 diagnosis |
| `view_file` limit 150 lines; `stored_observation_truncated` flag; `tool_agent` first-message text | Changed Week 2 behaviour (restored to `week2-complete`) |
| `gemini-3.1-flash-lite` (config, limits, pilot) | The models are the three in the prompt |
| 31B reliability probe, reliability-by-hour report, `scripts/diagnose_pilot.py`, `pilot_quota_analysis.py`, `probe_*` scripts except `probe_gemini_api.py` | Diagnostics |
| Flash-Lite `high` check, its scheduler (`run_when_quota.py`), `model_overrides` (D22) | Thinking stays at each vendor's default (D10) |
| `countTokens` (provider method, exact check, `token_counting` guard, D13/D20) | The prompt uses the estimator and the API's reported usage |
| `summary_omitted` fallback and event (D18) | It changed strategy behaviour; a summary that alone exceeds B is now a `context_failure` |
| Preserved-thinking share reporting, `missing_reasoning`/nudge accounting | Secondary metrics |
| Heartbeat, PID files, container recording, `--models`, global per-attempt log, progress hook | Operations tooling added after the prompt |
| The 5xx in-place retry policy (30 s–600 s, 60 min, 20 failures) | Restored to 6 attempts with jittered backoff, then `api_failure` |
| `scripts/jobs.py` | Replaced by the tmux/nohup instructions in RUNBOOK.md (the no-`pkill` rule is kept as text) |

**Kept on purpose, although added after the prompt, because the prompt's requirements need them:**
the cross-process lock on the quota ledger (2.2), per-attempt trajectories and the cap of 3
`api_failure` attempts per cell (2.5; it must be stated in the pre-registration's exclusion
rules), the active-time cap with a separate elapsed safety cap (2.4, because 2.2's sleep must not
count), `replay_tokens` for Gemma's re-billed thinking (2.3: B must hold), and safe reads of
episode records while another worker appends (2.5).

## Effect on the agent

The post-cleanup agent is the **first-pilot agent**: its system prompt, tool-result handling and
view limits are those of the first pilot, which hit the 75-step cap with no source edit in 5 of 5
finished episodes. Any later pilot is therefore **not read as new evidence** about the Gate 1 fixes.
Known consequence: a summary that alone pushes the minimal view over B ends the episode as
`context_failure` (a Hypothesis-found case, pinned in `tests/test_context.py`).

## Where things went

* `configs/archive/`: `pilot.yaml`, `repilot.yaml`, `pilot_gemini31.yaml`, `flash_thinking_high.yaml`
* `data/archive/pilots/`: episodes, trajectories and diffs of the four pilots (the process files
  `jobs/`, `pids/`, `heartbeat/` were not kept). It was scanned for secrets before it was
  committed. **All analysis excludes everything under `archive/`.**
* DECISIONS.md: each archived decision carries an "Archived (cleanup 2026-11-11)" note; no entry
  was deleted.

## Restoring something

```bash
git fetch --tags
git show week3-archive-2026-11-11:path/to/file > path/to/file     # one file
git checkout week3-archive-2026-11-11 -- scripts/jobs.py          # or check it out in place
git log --oneline week2-complete..week3-archive-2026-11-11        # find the commit that added it
```

An archived experiment file re-runs only together with the code of that tag (the archive configs
use `model_overrides` and retry fields that the current code no longer reads).
