# Week 3: Gate 1 report

*Status: **interim**, updated 2026-11-09 at 13:35 UTC.*

* **Done:** the Gate 1 decisions (pilot stopped, fixes approved).
* **Running:** the re-pilot (§9), and the Gemma 31B reliability probe (§6), which ends at about
  00:30 UTC on 10 November.
* §9 is completed when the re-pilot ends; the probe results come to Gate 2 as agreed.

## Summary

* **The build (Phase 1, §2.1–2.6) is complete and tested.**
  * 397 unit tests pass (301 at the start of the week), plus 5 Docker integration tests and 9
    manual live-API tests.
  * CI is green on `week3` (run 42625990988).
* **The harness changes don't affect grading or anti-cheating.**
  * The Oracle and Cheater reruns on the 25 dev tasks met every expectation.
  * The Docker equivalence test passes after the agent fixes too.
* **The first pilot was stopped early, at 13:03 UTC, by decision.** It showed a floor effect:
  every finished episode reached the 75-step cap with no source edit. The causes were
  diagnosed (§5) and the approved fixes are implemented (D19).
* **Gemma bills its earlier thinking twice.** It is kept, as the Gemma 4 model card requires
  for tool-call turns, and counted exactly (D21). That also fixes our limiter under-reserving
  TPM.
* **`countTokens` is out of all runs (D20).** The dashboard cross-check reconciles our ledger
  exactly for Flash-Lite, and shows `countTokens` doesn't count toward RPD.
* **Gemma 4 31B is unreliable**, with 57% of its attempts returning 5xx in the pilot. A 12-hour
  probe is running.
* **Proposals still open:** Flash-Lite's thinking level (§7), and the E3 reliability rule for
  31B (§6.3).

## 1. What was built

| Area | What it is | Decisions | Tests |
|---|---|---|---|
| Providers | google-genai `generateContent`, native function calling, thought signatures replayed verbatim, SDK retries off; OpenAI-compatible provider; per-call tokens, latency, finish reason, HTTP status, attempt log; exact limiter and backoff waits; re-billed thinking marked per reply | D8–D10, D12, D21 | `test_providers.py` (16), live (9) |
| Rate limiting | per-model RPM, TPM and RPD at a 90% margin; persistent daily ledger reset at midnight Pacific; 429 and 5xx handling that tells per-minute from daily limits | D11, D20 | `test_ratelimit.py` (13) |
| Context | four strategies under B; head+tail clip; online-calibrated estimator, including Gemma's re-billed thinking; invariant property tests | D12, D14, D15, D18, D21 | `test_context.py` (13) |
| Episode limits | 75 steps; the wall-clock cap counts active time only; 6 h elapsed safety cap, counted as `api_failure` | D17 | fake-clock tests |
| Agent | function calling through the environment API only; required reasoning, an accurate per-strategy view description, `[step k/N]`, a final-steps nudge, 150-line views | D9, D19 | `test_tool_agent.py` (21), Docker equivalence |
| Runner | `repogym run --experiment [--models]`, resumable; every attempt kept and numbered; ≤ 3 `api_failure` attempts per cell; heartbeat; `repogym status` with running episodes | D16 | `test_experiments.py` (26), `test_cli_week3.py` (3) |
| Accounting | tokens, requests, summary overhead, cost, budget violations, estimation error, waiting and API time, `summary_omitted`, `missing_reasoning`, reminders | D17–D19 | `test_tool_agent.py` |
| Logging | secret redaction on every log event | — | `test_logs.py` (4) |

* **Oracle and Cheater reruns** after the harness changes, on the 25 dev tasks (`ff3aad3`, clean
  tree):
  * **Oracle:** 25/25 solved, no flags (`data/runs/week3-dev-oracle`).
  * **Cheater:** 0/25 solved, all 25 flagged (`data/runs/week3-dev-cheater`).
* **Pilot hygiene:** both pilots are dev-only and tagged `pilot` in provenance. The guard is
  tested against both committed files: they are refused as soon as any test-split task is
  added.

## 2. Verified API facts

Checked 2026-11-09. Details are in DECISIONS.md D8–D13 and D20–D21.

| Fact | Source |
|---|---|
| TPM counts **input** tokens; "quotas reset at midnight Pacific time" (that's 08:00 UTC from 1 November); limits apply per project, per model | https://aistudio.google.com/docs/rate-limits.md |
| `generateContent` is "legacy" but supported | https://aistudio.google.com/llms.txt |
| The first `functionCall` of each step in the current turn must carry its signature; documented dummy signatures skip validation | https://aistudio.google.com/docs/thought-signatures.md?generate-content=true |
| Gemini 3.x: don't change the sampling defaults. Flash-Lite thinking levels are `minimal` (default), `low`, `medium` and `high`. "Return the entire response with all parts" | https://aistudio.google.com/docs/thinking.md?generate-content=true |
| Gemma 4: temperature 1.0, top_p 0.95, top_k 64; cutoff Jan 2025; "thinking content should be preserved" in tool-call turns | https://ai.google.dev/gemma/docs/core/model_card_4 |
| `countTokens` takes either `contents` or `generateContentRequest`, "never both" | https://aistudio.google.com/api-reference/tokens/count-tokens.md |

**Measured:**

* Multi-turn tool calling, signature replay, a dropped middle step and a summary part all
  work on all three models.
* **Gemma returns its reasoning as a `thought: true` text part, billed at twice its thinking
  tokens when replayed (D21).**
* `countTokens` isn't counted against RPM, TPM or RPD (D13, D20).

## 3. First pilot (stopped early)

* **Design:** 3 dev tasks (click-3473, astroid-3057, isort-2683) × 3 models × `obs_mask`,
  B = 12,000.
* **Run:** started 11:06 UTC; **stopped 13:03 UTC** by operator decision.
* **Why it was stopped:**
  * everything still running used an agent known to be broken (§5);
  * 26B's throughput data and the thinking re-billing evidence were already in hand;
  * 31B was retrying click with no limit, and would never have reached its other tasks.
* **How:** SIGINT to the runner, then removal of exactly the two containers of the episodes
  in flight. Two Week 2 containers from 7 November, created but never started, were left
  alone.
* **Abandoned in flight, no record:** 26B on astroid-3057 (about 31 min in), and 31B click
  attempt 3 (about 6 min in).

| Cell | Termination | Reward | Steps | Elapsed | Active | Waiting | Input tokens |
|---|---|---|---|---|---|---|---|
| flash-lite · click-3473 | step_cap | 0 | 75 | 5.9 min | 3.3 min | 2.6 min | 773,973 |
| flash-lite · astroid-3057 | step_cap | 0 | 75 | 13.0 min | 13.0 min | 0 | 608,911 |
| flash-lite · isort-2683 | step_cap | 0 | 75 | 8.5 min | 7.6 min | 0.9 min | 524,601 |
| gemma-26b · click-3473 | step_cap | 0 | 75 | 84.5 min | 14.4 min | 70.1 min | 755,664 |
| gemma-31b · click-3473 (attempt 1) | api_failure | 0 | 29 | 42.5 min | 28.3 min | 14.2 min | 175,029 |
| gemma-31b · click-3473 (attempt 2) | api_failure | 0 | 27 | 65.4 min | 32.6 min | 32.8 min | — |
| gemma-26b · astroid; 31B attempt 3 | abandoned at stop | | | | | | |
| 26B isort; 31B astroid, isort | never started | | | | | | |

* **Budget violations:** Flash-Lite 0 of 225 calls. 26B 21 of 75, all explained by re-billed
  thinking (D21).
* **`summary_omitted`:** 0, as expected, since `obs_mask` has no summary.
* **Pilot data:** `data/runs/week3-pilot`. It is excluded from all later analysis. 31B's
  attempt-1 trajectory was overwritten by attempt 2; that bug is fixed (D16).

## 4. Token counting, quotas, and the dashboard (D20, D21)

### 4.1 Decisions

* **The `countTokens` dashboard test is cancelled.** `countTokens` is not used in the agent loop
  or any timed run; experiments asking for it are refused at load. It stays for offline
  validation only.
* **Our local quota ledger is the source of truth for our own request counts.**
* **Gemma's earlier thinking is kept and counted exactly (§4.3).**

### 4.2 Dashboard cross-check

Dashboard figures were supplied by the operator: 28-day peaks covering today.

**RPD:**

| Model | Dashboard | Ledger | − 18 blocked locally | + sent outside the limiter | Expected | Difference |
|---|---|---|---|---|---|---|
| Flash-Lite | 247 | 252 | 234 | +13 | **247** | **0** |
| Gemma 26B | 168 | 184 at 13:02 | 166 | +10 | 176 | −8 |
| Gemma 31B | 159 | 172 at 13:02 | 154 | +8 | 162 | −3 |

* **The "blocked locally" correction.** The first live-test run's attempts were blocked by the
  unit-test network guard and never left the machine: 3 tests × 6 attempts per model. Our
  ledger over-counts by these 18 per model.
* **The "sent outside the limiter" correction.** These calls went straight to the SDK and so
  aren't in the ledger:
  * the morning probes, 8 generation calls per model;
  * 5 Flash-Lite debugging calls;
  * 2 Gemma 26B prototype calls.
* **Flash-Lite matches exactly.** Its count stopped changing at 11:35, so the comparison doesn't
  depend on when the dashboard was read.
* **The Gemma differences** fit a dashboard reading 5–10 minutes before 13:02, while both were
  still sending 0.7–1.3 requests a minute.
* **`countTokens` isn't in RPD.** Flash-Lite made 25 `countTokens` calls and still matches
  exactly. 26B made about 165, more than its dashboard RPD can contain.

**TPM:**

* The dashboard shows peaks of 19.65K (26B) and 18.34K (31B) against a 16K limit.
* I rebuilt the pilot's input tokens per calendar minute (`scripts/pilot_quota_analysis.py`).
  Both Gemma models went over 16K in Google's accounting.

| Model | Rebuilt peak minute | Largest single call | Minutes over 16K | Dashboard peak |
|---|---|---|---|---|
| Flash-Lite | 148,403 | 11,771 | 0 / 27 (limit 250K) | 172.99K |
| Gemma 26B | 16,943 | 16,943 | 3 / 72 | 19.65K |
| Gemma 31B (attempt 2 only) | 11,962 | 11,962 | 0 / 25 | 18.34K |

* **Each Gemma 26B overrun was a single call.** Its billed size went past both B and 16K
  because re-billed thinking wasn't in our estimate. The limiter admitted calls on that
  estimate, so it under-reserved.
* **The dashboard peaks are higher than ours.** That fits Google also counting the input of
  failed 5xx attempts, which trajectories don't record, and 31B's lost attempt 1.
* **31B's 500/503s did not coincide with over-limit minutes.**
  * 0 of 17 calls that hit a 5xx came in a minute already over the limit. Most came at the
    start of a minute with nothing else sent yet.
  * Its errors are server-side, not quota-driven.
* **Fix (D21; implemented).**
  * The estimate now includes re-billed thinking, and the context manager keeps every prompt
    within B = 12,000. That is below the 14,400 effective TPM.
  * The limiter still admits on the estimate and settles with the reported tokens.
  * A failed attempt's reservation stays in the window, so a retry in the same minute waits
    instead of doubling the input.

### 4.3 Gemma's earlier thinking: removal tested, kept by the docs

Same third call, 26B, after two real tool-calling steps that thought 1,244 and 10 tokens
(`scripts/probe_thinking_history.py`):

| Variant | HTTP | Billed prompt |
|---|---|---|
| Full replay | 200 | 3,139 |
| Signatures replaced by the documented dummy, or removed | 200 | 3,139 |
| **Thought parts dropped, signatures kept** | 200 | **631** (older reply only: 651) |

* **The test.** Dropping the thought text saves exactly **2 × its thinking tokens** (2,508 and
  2,488), and the API accepts it.
* **What the docs say.**
  * The Gemma 4 model card says thinking content "should be preserved" in tool-call turns, and
    our whole episode is one tool-call turn.
  * The Gemini docs say to return the entire response.
  * Without its earlier thoughts the model also re-thinks more: 899 and 409 tokens on the next
    call, against 10.
* **Decision (per your rule):** keep the thoughts and count them exactly.
* **Consequence.** Under B, Gemma's own reasoning competes with observations, so its drop
  fallback fires earlier. This is reported, not hidden.

## 5. Floor-effect risk

### 5.1 Evidence on why first-pilot episodes failed

The data is all 4 finished episodes plus 31B's surviving trajectory
(`scripts/diagnose_pilot.py`, read-only).

| | flash · click | flash · astroid | flash · isort | 26B · click | 31B · click (att. 2) |
|---|---|---|---|---|---|
| Source edits | **0** | **0** | **0** | **0** (2 repro scripts) | 0 |
| `run_tests` / `submit` | 0 / 0 | 1 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Exact repeated actions (first at step) | 12 (23) | 24 (27) | 21 (12) | 10 (18) | 1 |
| …of which the previous result was **masked** | 5 | **22** | **19** | 2 | 5 masked re-reads |
| Re-reads of a file after its earlier read was masked | 60 | 29 | 33 | 37 | 12 |
| Observations clipped | **58/75** | 17/75 | 8/75 | 7/75 | 3/29 |
| Characters hidden by the clip | 48% | 86% | 23% | 26% | 46% |
| Replies with any reasoning text | **0/75** | **0/75** | **0/75** | **0/75** | 0/29 |
| Thinking tokens | 0 | 0 | 0 | 11,191 | 1,587 |
| Gold file found and read | yes | **no** | yes | yes | yes |

1. **No planning carried across steps (the main cause).** Neither model wrote any text with its
   calls. Flash-Lite at `minimal` thinks 0 tokens. Assistant messages are the one part of
   history that is never masked, and with no text in them, nothing learned survives masking.
   The Week 2 bash agent, which solved isort-2683 in 88 and 46 steps, required `THOUGHT:`.
2. **The clip made big reads useless.** 400-line windows of click's `core.py` are 9,000–17,000
   characters; the clip keeps about 4,700. 71 of 75 calls were `view_file`.
3. **Masking with M = 5 drives repetition.** 22 of 24 and 19 of 21 repeats on astroid and isort
   came right after the earlier identical result was masked. For example,
   `git grep -n "Self" astroid/` (2,345 characters, never clipped) ran 8 times.
4. **Gemma 26B explored without converging:** reproduction scripts that crash, and empty greps.
5. **The tools aren't the bottleneck.** No `str_replace` was attempted; tool errors were few.
6. **Prompt clarity.** How to submit was clear. What was missing: when to stop exploring, the
   step budget, and what the model can see.
7. **More steps wouldn't have helped.** Loops started between steps 12 and 27, and no episode
   was near a fix.

### 5.2 Fixes (approved and implemented, D19)

* **Reasoning required with each tool call.** A reply without it still runs. It is recorded as
  `missing_reasoning`, and every second one in a row adds one short reminder. Both are counted
  per episode.
* **An accurate view description,** generated per strategy:
  * the clip size;
  * masking after the last 5 results;
  * oldest-first removal;
  * the summary.

  The misleading 200,000-character line is gone for this agent.
* **`[step k/75]`** after each result, plus one nudge when 15 calls remain.
* **`view_file`** shows at most 150 lines.
* **Not changed:** B, the clip, M, K, the summary length, and the 75-step cap. The re-pilot
  tests the fixes alone.

### 5.3 Estimate

* With these fixes, I expect a dev pass rate of roughly **10–25%** for Flash-Lite and Gemma
  26B, with low confidence. It rests on two things:
  * the Week 2 bash agent's 1 of 3 tasks with unbounded context;
  * the first pilot's failures being agent-design failures, not wrong fixes.
* **Stop rule (yours):** if fewer than 1 of the 6 re-pilot episodes solves its task, or the
  repetition persists, I stop and report before Gate 2.

## 6. Gemma 4 31B reliability, and the E1 workhorse

### 6.1 Pilot error record

| Attempt | Window (UTC) | Steps | Retries | 500 | 503 | Ended by | Median / max API latency |
|---|---|---|---|---|---|---|---|
| 1 | 11:07–11:50 | 29 | 25 | (trajectory lost) | | `ProviderUnavailable`: one call failed 6 attempts | — |
| 2 | 11:51–12:56 | 27 | 36 | 29 | 7 | `ProviderUnavailable`: one call failed 6 attempts | 44 s / 182 s |
| for comparison: 26B | 11:07–12:32 | 75 | 15 | 15 | 0 | step cap | 5 s / 49 s |
| for comparison: Flash-Lite | | 225 | 0 | 0 | 0 | step cap | 2–8 s / 21 s |

* In attempt 2, 57% of 31B's attempts returned a 5xx. There were zero 429s.
* The 6 API probes earlier in the day also returned 500 INTERNAL.
* My 4 thinking-removal runs on 26B saw intermittent 500s too (1–3 per run), always recovered.
  So 26B isn't immune, just far less affected.

### 6.2 Reliability probe (finished)

* **Design:** 20 single-attempt calls to `gemma-4-31b-it` (a tiny prompt, 256 output tokens, no
  retries), one every 36 minutes, from 13:03 UTC on 9 November to 00:27 UTC on 10 November.
  Results are in `docs/week3/evidence/reliability_gemma-4-31b-it.jsonl`.
* **Result: 8 of 20 calls succeeded (40%).** 12 failed: 9 with HTTP 500 and 3 with HTTP 503.
  * **By time (UTC):** 13:00–18:00 had 6 failures in 9 calls, 18:00–24:00 had 6 in 10 calls,
    and the last call, at 00:27, succeeded.
  * **Streaks:** the longest run of consecutive failures was 4 calls, about 2.5 hours.
  * **Latency** was high even when it worked: successful calls took 15–36 s (median 19 s) to
    answer a one-word prompt. Failures came in two kinds: instant 500s (1–1.5 s, five of them)
    and slow ones (23–56 s).
* **What it means:** this is a service-side problem that lasts across hours. It isn't a quota
  effect (one call per 36 minutes is far under every limit) and it isn't load from our other
  runs. At a 60% single-attempt failure rate, the in-place retry rule (D16) makes a 75-step
  episode very slow, and a 20% cell-failure exclusion threshold is likely to be exceeded.
* **Caveat:** one day, one project. The overnight hours had no better rate than the afternoon,
  but this doesn't show how it behaves on other days.

### 6.2b Candidate replacement: gemini-3.1-flash-lite (pilot pending)

* **Model ID**, verified with `models.list` on 2026-11-09: `models/gemini-3.1-flash-lite`,
  version `3.1-flash-lite-05-2026`. It's the stable release; a `-preview` also exists.
* **Limits** (operator dashboard): 15 RPM, 250K TPM, 500 RPD.
* **Thinking:** `high`, per D22. The Gemini thinking docs list "Gemini 3.5 & 3.1 Flash-Lite"
  together, with `minimal` as the default and `high` supported.
* **Live tests (9 November):** all passed.
  * The first function call carries a signature.
  * A 3-step loop with signatures replayed works (bash, bash, submit).
  * A dropped middle step plus a summary part is accepted.
  * `countTokens` gave 519 against 520 reported (offline check only).
* **Pilot:** the same 3 dev tasks as the re-pilot, from 08:05 UTC on 10 November, on its own
  quota (`configs/experiments/pilot_gemini31.yaml`).
* *Results go here, beside the 31B probe, for the replace-or-keep decision at Gate 1.*

### 6.3 Pre-registered reliability rule for E3

This goes into PREREGISTRATION.md at Gate 2:

* A cell gets at most 3 `api_failure` attempts (now enforced by the runner, D16).
* **If more than 20% of 31B's test cells end as `api_failure` after 3 attempts, 31B is
  reported as "incomplete, excluded from ranking",** with its partial results in an appendix.

### 6.4 E1 workhorse: Gemma 4 26B

* It had no failed episodes; its 17% of 5xx attempts all recovered.
* Its median latency is 5 s, and its RPD (14,400) is ample.
* Its limit is TPM: about 1 call a minute at full context, so about 85 min per 75-step episode.
* **Not 31B:** 2 of 2 episodes failed.
* **Not Flash-Lite:** 450 requests a day is about 6 full episodes, so E1 would take about 17
  days.

### 6.5 Server errors by hour, and in-place retries (D16, amended)

Error rate per model by UTC hour, 9 November (`scripts/reliability_by_hour.py`):

| Model | Hour (UTC) | Attempts | Server errors | Rate |
|---|---|---|---|---|
| gemini-3.5-flash-lite | 2026-11-09 11:00 | 225 | 0 | 0% |
| gemma-4-26b-a4b-it | 2026-11-09 11:00 | 58 | 8 | 14% |
| gemma-4-26b-a4b-it | 2026-11-09 12:00 | 32 | 7 | 22% |
| gemma-4-26b-a4b-it | 2026-11-09 13:00 | 57 | 27 | 47% |
| gemma-4-26b-a4b-it | 2026-11-09 14:00 | 72 | 33 | 46% |
| gemma-4-26b-a4b-it | 2026-11-09 15:00 | 41 | 11 | 27% |
| gemma-4-31b-it | 2026-11-09 11:00 | 20 | 11 | 55% |
| gemma-4-31b-it | 2026-11-09 12:00 | 55 | 37 | 67% |
| gemma-4-31b-it | 2026-11-09 13:00 | 2 | 1 | 50% |
| gemma-4-31b-it | 2026-11-09 14:00 | 2 | 2 | 100% |
| gemma-4-31b-it | 2026-11-09 15:00 | 1 | 1 | 100% |

* **Sources.** Attempts come from trajectories, from the 6 failed attempts behind each
  `ProviderUnavailable` episode, from the 31B probe and from the thinking experiments. From
  15:37 UTC they come from the new per-attempt log, `data/quota/attempts-<day>.jsonl`.
* **What one day shows.** 26B's rate tripled between 11:00–12:00 UTC and 13:00–14:00 UTC
  (14–22% → 46–47%), then eased (28% at 15:00). 31B is at 55–100% in every hour. Flash-Lite
  had none.
* One afternoon can't separate time-of-day from day-to-day variation. The attempt log now
  records every attempt, so by Gate 2 there will be several days of hourly rates to schedule
  E1–E3 around, if they show a pattern.
* **The 31B probe** (single attempts every 36 min): 500, 200, 500, 500, 503 so far, so 1 of 5
  succeeded.

**How 5xx are handled now (from 15:37 UTC):**

* A 5xx or transport error is retried in place: same episode, sandbox and state.
* Backoff is jittered and exponential, from 30 s, capped at 10 min between tries.
* `api_failure` comes only after 20 consecutive failures or 60 minutes, whichever comes first.
  At these values 60 minutes always comes first, after about 10–11 failures.
* The waiting is excluded from active time and bounded by the 6-hour elapsed cap.
* The thresholds are the experiment's `retry` section: hashed, frozen and recorded per
  episode.

**Applied too late for attrs-1448.** Its three attempts all ran under the old rule:

| Attempt | Outcome | Steps |
|---|---|---|
| 1 | `api_failure` (5xx) | 9 |
| 2 | `api_failure` (5xx) | 34 |
| 3 | submitted, unsolved | 41 |

Its outcome stands as recorded. tomlkit-536 restarted from scratch under the new rule.

## 7. Flash-Lite thinking level (proposal, evidence from the docs)

* **Status.** Flash-Lite ran with 0 thinking tokens at its default (`minimal`). Thinking-level
  options are `minimal` (default), `low`, `medium` and `high`; `minimal`, `low` and `high` were
  accepted when probed.
* **What the docs say**
  (https://aistudio.google.com/docs/thinking.md?generate-content=true, checked 2026-11-09):

  | Level | Docs' description |
  |---|---|
  | `minimal` | "Minimizes latency for chat or high throughput applications"; "may think very minimally for complex coding tasks" |
  | `low` | "Best for simple instruction following, chat, or high-throughput applications" |
  | `medium` | "Balanced thinking for most tasks" |
  | `high` | "Dynamic" |

  The task-type guidance is explicit: "Hard Tasks… such as solving complex math problems or
  **coding tasks**, we recommend setting a high thinking budget." The same page calls
  `thinkingLevel` "recommended for Gemini 3 models".
* **Proposal: pin `thinking_level: high` for Flash-Lite in E3,** following the docs'
  recommendation for coding tasks.
  * **Costs:**
    * more output tokens (billed only for the list-price figure; ₹0 on the free tier);
    * higher latency;
    * no effect on RPD;
    * TPM counts input only.
  * **Unknown:** whether Gemini re-bills thinking the way Gemma does. Gemini doesn't return
    thought text unless asked, so it probably doesn't, but the accounting will show it.
* **Not in the re-pilot.** The re-pilot keeps `minimal`, so its result reflects only the
  approved agent fixes.
* **If you approve `high`,** I suggest one more 3-episode Flash-Lite dev check after the
  re-pilot (about 225 requests, its own day) before freezing.

## 8. Harness gaps (fixed, D16)

* **Retries never overwrite.** Each attempt is named `<cell>__a<n>` with a collision-safe
  suffix. Earlier attempts are listed in provenance, and `repogym status` reports attempts,
  cells retried and cells given up.
* **Heartbeat.** `repogym status` shows each worker's state, the running cell and attempt, its
  current step, minutes in, and seconds since the last model call. Verified on the re-pilot.
* **Unbounded `api_failure` retries** are replaced by the 3-attempt cap.

## 9. Re-pilot (running)

* **Design:**
  * 3 dev tasks not used before, stratified by a fixed rule: attrs-1448, tomlkit-536,
    click-3504;
  * × Flash-Lite and Gemma 26B, `obs_mask`, B = 12,000, 75 steps;
  * tagged `pilot` and excluded from analysis (`configs/experiments/repilot.yaml`).
* **Quota and schedule:**
  * Flash-Lite had 198 requests left today under our 90% margin, against up to 225 needed. So
    its three episodes start at **08:05 UTC on 10 November**, five minutes after the
    midnight-Pacific reset. A detached scheduled process handles that.
  * Gemma 26B started at 13:30 UTC on 9 November.
* **Flash-Lite `high` check:** it starts as soon as all the re-pilot's Flash-Lite cells are done
  (`scripts/run_when_quota.py`). It doesn't wait for a day with enough quota free.
* **If the quota runs out mid-episode,** the call is held in place until the 08:00 UTC reset
  and then continues (D16). I need to correct what I implied earlier: before today's change,
  running out of daily quota *ended* the episode as `api_failure`, and the cell was rerun from
  scratch after the reset. The in-place hold is new. These are confirmed by unit tests:
  * a call held 20 hours across the reset continues in the same episode;
  * the hold counts neither as active time nor toward the 6-hour elapsed cap.

  The sandbox container survives the hold because it runs `sleep infinity`. That part is
  confirmed by its configuration, not by a live 20-hour hold yet. Whether the `high` check
  actually crosses a reset will show in its record (`episode_seconds.quota_pause`).
* **Live confirmation of the quota hold:** *to be filled from the 10 November runs.* I'll report
  whether any episode actually crossed the 08:00 UTC reset in a hold, using its
  `episode_seconds.quota_pause`, its single attempt record, and its container name staying the
  same in the heartbeat. If no run triggers a hold naturally, I'll say so; I won't force one.
* *Results: to be completed when the runs finish:* solves, missing-reasoning events,
  reminders, repetition (exact and after masking), budget violations, the drop fallback,
  preserved-thinking share, steps, and time.

### 9.1 Gemma 26B half (finished 9 November, 19:21 UTC)

| Cell | Attempt | Termination | Steps | Source edited | `str_replace` (failed) | `run_tests` / `submit` | Exact repeats (after a mask) | Drop fallbacks | Preserved thinking, median / max share of prompt |
|---|---|---|---|---|---|---|---|---|---|
| attrs-1448 | 1 | api_failure (5xx, old rule) | 9 | no | 0 | 0 / 0 | 0 (0) | 0 | 6% / 24% |
| attrs-1448 | 2 | api_failure (5xx, old rule) | 34 | `validators.py` | 6 (4) | 2 / 0 | 6 (1) | 19 | 53% / 65% |
| attrs-1448 | 3 | **submitted, unsolved** | 41 | `validators.py` | 9 (6) | 1 / 1 | 16 (1) | 22 | 47% / 58% |
| tomlkit-536 | 1 | **context_failure** | 18 | no | 0 | 0 / 0 | 6 (2) | 0 | 10% / 38% |
| click-3504 | 1 | api_failure (5xx, new rule: 11 errors over 60 min) | 4 | no | 0 | 0 / 0 | 0 (0) | 0 | 10% / 12% |
| click-3504 | 2 | step_cap | 75 | no (5 scratch scripts) | 0 | 0 / 0 | 17 (1) | 69 | 40% / 71% |

**Solves: 0 of 3 cells.** The Flash-Lite half runs on 10 November.

**What the fixes changed (against the first pilot):**

* **Reasoning:** text with every call in every episode (0 `missing_reasoning` events, against
  0 of 254 replies with text before).
* **Masking-driven repetition is gone:** 1–2 repeats after a mask per episode, against 19–22
  before. The repeats left are re-runs of the agent's own scratch scripts and greps, made
  while the earlier result was still visible.
* **The agent now edits, tests and submits.** attrs-1448 attempt 3 edited
  `src/attr/validators.py`, the right file, and submitted. The hidden tests failed.

**What's still wrong:**

1. **Preserved thinking crowds the budget (D21).**
   * In the long episodes Gemma's own earlier thinking is a median of 40–53% of each prompt,
     peaking at 71%. The drop fallback fires 19–69 times per episode, so older observations
     are dropped to make room.
   * **It also caused tomlkit-536's `context_failure`.** One reply thought 5,385 tokens. Its
     replayed thought text (about 10,770 billed tokens) belongs to the latest unit, which
     can't be dropped. With the prefix and the clipped observation, the prompt needed about
     14.7K tokens against B = 12,000.
   * Under D21 (keep the thoughts, as the Gemma docs say), a single long-thinking reply can
     make the next prompt impossible to fit.
2. **`str_replace` friction:** 10 of 15 failed. Mostly `old` not matching the file's
   whitespace (the agent reconstructs code it can no longer see); once `old` and `new` were
   identical. Separately, `run_tests` was called once without its required `target`.
3. **click-3504 attempt 2 spent its 75 steps on 5 scratch scripts** and never edited the
   source. It got the final-steps nudge at step 60.

**Decisions this needs at Gate 1.** These are options, not yet implemented:

* **A. Preserved thinking that can't fit.** If the latest reply's preserved thinking alone
  makes the minimal prompt exceed B, then as a last resort drop the thought text of the
  *older* replies first. If that still isn't enough, drop it from the latest reply too.
  Record a `thoughts_dropped` event, count it per episode, and report it beside
  `summary_omitted`.
  * This departs from the Gemma docs only when following them makes the episode impossible.
  * Alternatives:
    * accept `context_failure` as the outcome;
    * give Gemma a larger B (its effective TPM is 14,400, so B could rise to about 14,000 at
      most);
    * set Gemma's thinking to `minimal`, which conflicts with D22.
  * **I recommend A**, visible and counted, the same way `summary_omitted` was handled.
* **B. `str_replace` failure message.** Show the closest matching lines of the file (with line
  numbers) when `old` isn't found, so the agent can correct its whitespace without re-reading.
  This is a tool-message change only; the semantics stay strict.
* **C. `run_tests` default.** When `target` is missing, return the error plus the list of
  test files that exist, rather than just "missing required argument". Message only.

**Bug found and fixed.** When a call finally failed after 60 minutes of 5xx retries, its
waiting wasn't reported, so it counted as active time: click-3504 attempt 1 showed 3,751 s
"active" over 4 steps. Errors now carry their waiting, and the agent reports it before the
episode ends (tested). Fixed before the 10 November runs.

## 10. Time estimates

These are based on pilot throughput. They will be revised with the re-pilot's steps per
episode.

| Experiment | Size | Binding limit | Estimate |
|---|---|---|---|
| E1 | 26B × 4 strategies × 25 dev tasks = 100 episodes | 26B TPM, ~1 call/min | ~3.5 days at ~50 calls per episode; ~6 days at 75 |
| E2 | winner × 10 tasks × 3 repeats = 30 episodes | 26B TPM | ~1 day |
| E3 | 3 models × 48 test tasks | Flash-Lite RPD: ~2,400 requests at 450/day | ~5.5 days, with 26B and 31B in parallel |

## 11. Parameter summary

| Parameter | Value | Status |
|---|---|---|
| Agent prompt, view text, step counter, nudges, reasoning events | D19 | implemented |
| `view_file` max lines | 150 | implemented |
| Prompt sizing | estimator + Gemma's re-billed thinking; no `countTokens` | implemented (D20, D21) |
| `api_failure` attempts per cell | 3 | implemented |
| `max_wall_seconds` (active) / `max_elapsed_seconds` | 3,600 / 21,600 | unchanged |
| B, clip, M, K, summary length, `max_steps` | 12,000 / 2,000 / 5 / 4 / 1,000 / 75 | unchanged for the re-pilot |
| Flash-Lite `thinking_level` | `minimal` → proposed `high` | **needs your decision** (§7) |
| E3 31B rule | §6.3 | to pre-register at Gate 2 |

I will stop for your approval once §9 is complete.
