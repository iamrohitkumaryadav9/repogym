# Week 3: Gate 1 report

> **Historical (cleanup 2026-11-11).** This report describes the code and runs as they were at
> tag `week3-archive-2026-11-11`. Much of what it proposes or reports (the D19 agent, the 3.1
> Flash-Lite pilot, the `high` check, the 31B probe, the 5xx in-place policy) has since been
> archived; see [ARCHIVE.md](ARCHIVE.md). Its pilot data is in `data/archive/pilots/`.

*Status: **complete, waiting for your decision**, updated 2026-11-11 at about 02:00 UTC.*

* **Done:** the re-pilot (§9, 0 of 6 episodes solved), the Flash-Lite `high` check (§7.1), the
  3.1 Flash-Lite pilot (§6.2b), and the Gemma 31B reliability probe (§6.2).
* **Stop rule reached.** Fewer than 1 of the 6 re-pilot episodes solved, so I'm stopping here
  as you asked, before Gate 2. §9.3 lists what I found and the decisions I need.

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
* **Gemma 4 31B is unreliable:** 57% of its pilot attempts returned 5xx, and the 12-hour probe
  succeeded on 8 of 20 calls (§6.2).
* **The re-pilot did not solve anything: 0 of 6 episodes (§9).** The agent now reads, edits,
  tests and submits, and in 7 of the 12 final episodes across the four runs it edited the file the
  gold patch changes. But no episode passed the hidden tests. The stop rule applies.
* **Gemini thinking adds billed prompt tokens our estimator doesn't count** (§9.2). With
  thinking on, 3.1 and 3.5 `high` ran over B in 97 calls (3.1: 42, 3.5 `high`: 55); with
  3.5 `minimal`, 0. This needs a fix before any frozen run.
* **3.1 Flash-Lite is the most reliable model tested** (0 of 183 attempts failed) but it
  never writes reasoning text, whatever the reminders (§6.2b).
* **Decisions needed:** §9.3.

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

### 6.2b Candidate replacement: gemini-3.1-flash-lite (pilot finished)

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
* **Pilot:** the same 3 dev tasks as the re-pilot, on its own quota
  (`configs/experiments/pilot_gemini31.yaml`). Its start slipped from 08:05 UTC on 10 November
  to 00:40 UTC on 11 November, because a WSL restart killed the scheduled jobs; nothing
  else changed.
* **Results** (thinking `high`, `obs_mask`, B = 12,000, 75 steps):

  | Task | Termination | Steps | Source edited | `str_replace` (failed) | Exact repeats | Over-budget calls | Time |
  |---|---|---|---|---|---|---|---|
  | attrs-1448 | submitted, unsolved | 33 | `validators.py` (not `.pyi`) | 5 (2) | 4 | 0 | 3.1 min |
  | tomlkit-536 | submitted at step 75, unsolved | 75 | `container.py` | 15 (4) | 18 | 15 | 6.6 min |
  | click-3504 | step_cap | 75 | `shell_completion.py` | 7 (1) | 14 | 27 | 6.4 min |

  * **Reliability:** 183 attempts, 183 returned 200; 0 server errors. Median latency per
    call 2.3–2.8 s.
  * **Reasoning text:** 183 of 183 replies had none (`missing_reasoning` 100%), with 90
    reminders sent. 3.1 puts its thinking in thought tokens (4.7K, 17.3K and 25.5K per
    episode) but doesn't write a visible explanation with the tool call. The D19 reminder
    doesn't change that.
  * **Budget:** 42 over-budget calls, up to 30% above the estimate (§9.2).
  * **Solves: 0 of 3.** In all three episodes it edited the gold file.
* **For the replace-or-keep decision:**

  | | Gemma 31B | 3.1 Flash-Lite |
  |---|---|---|
  | Server errors | 63% of 95 attempts (probe: 12 of 20 calls) | 0 of 183 |
  | Episode throughput | cells lost to `api_failure` | 3 episodes in 16 min |
  | Reasoning text with tool calls | not tested since D19 | **no** (100% missing) |
  | Solves on 3 dev tasks | not run in the re-pilot | 0 of 3 |
  | Free-tier RPD | 14,400 | 500 |

  * **Recommendation: replace 31B in E3 with 3.1 Flash-Lite.** 31B can't finish a cell
    reliably, and §6.3's rule would mark it "incomplete" almost certainly. 3.1 finishes every
    cell.
  * **Two costs of the swap,** both of which I'd record in DECISIONS.md:
    1. RPD is 500, the same limit as 3.5 Flash-Lite, and the two are separate quotas. 48 test
       tasks at about 75 requests each is about 3,600 requests, or roughly 7–8 days for 3.1
       alone, running alongside 3.5 and 26B.
    2. Two of the three models would be Flash-Lite variants from one family, which narrows
       the RQ2 comparison. Gemma 26B stays as the open-weights model.
  * I have **not** changed E1 or E3, as you asked.


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
| gemini-3.1-flash-lite | 2026-11-11 00:00 | 183 | 0 | 0% |
| gemini-3.5-flash-lite | 2026-11-09 11:00 | 225 | 0 | 0% |
| gemini-3.5-flash-lite | 2026-11-11 00:00 | 67 | 12 | 18% |
| gemini-3.5-flash-lite | 2026-11-11 01:00 | 349 | 15 | 4% |
| gemma-4-26b-a4b-it | 2026-11-09 11:00 | 58 | 8 | 14% |
| gemma-4-26b-a4b-it | 2026-11-09 12:00 | 32 | 7 | 22% |
| gemma-4-26b-a4b-it | 2026-11-09 13:00 | 57 | 27 | 47% |
| gemma-4-26b-a4b-it | 2026-11-09 14:00 | 72 | 33 | 46% |
| gemma-4-26b-a4b-it | 2026-11-09 15:00 | 58 | 18 | 31% |
| gemma-4-26b-a4b-it | 2026-11-09 16:00 | 17 | 12 | 71% |
| gemma-4-26b-a4b-it | 2026-11-09 17:00 | 55 | 21 | 38% |
| gemma-4-26b-a4b-it | 2026-11-09 18:00 | 49 | 21 | 43% |
| gemma-4-26b-a4b-it | 2026-11-09 19:00 | 21 | 8 | 38% |
| gemma-4-31b-it | 2026-11-09 11:00 | 20 | 11 | 55% |
| gemma-4-31b-it | 2026-11-09 12:00 | 55 | 37 | 67% |
| gemma-4-31b-it | 2026-11-09 13:00 | 2 | 1 | 50% |
| gemma-4-31b-it | 2026-11-09 14:00 | 2 | 2 | 100% |
| gemma-4-31b-it | 2026-11-09 15:00 | 1 | 1 | 100% |
| gemma-4-31b-it | 2026-11-09 16:00 | 2 | 1 | 50% |
| gemma-4-31b-it | 2026-11-09 17:00 | 2 | 1 | 50% |
| gemma-4-31b-it | 2026-11-09 18:00 | 1 | 0 | 0% |
| gemma-4-31b-it | 2026-11-09 19:00 | 2 | 2 | 100% |
| gemma-4-31b-it | 2026-11-09 20:00 | 2 | 2 | 100% |
| gemma-4-31b-it | 2026-11-09 21:00 | 1 | 0 | 0% |
| gemma-4-31b-it | 2026-11-09 22:00 | 2 | 1 | 50% |
| gemma-4-31b-it | 2026-11-09 23:00 | 2 | 1 | 50% |
| gemma-4-31b-it | 2026-11-10 00:00 | 1 | 0 | 0% |

* **Sources.** Attempts come from trajectories, from the 6 failed attempts behind each
  `ProviderUnavailable` episode, from the 31B probe and from the thinking experiments. From
  15:37 UTC they come from the new per-attempt log, `data/quota/attempts-<day>.jsonl`.
* **Totals to 11 November, 01:50 UTC:** gemini-3.1-flash-lite: 0/183 attempts were server errors (0%); gemini-3.5-flash-lite: 27/641 attempts were server errors (4%); gemma-4-26b-a4b-it: 155/419 attempts were server errors (37%); gemma-4-31b-it: 60/95 attempts were server errors (63%).
* **What 9 November shows.** 26B's rate tripled between 11:00–12:00 UTC and 13:00–14:00 UTC
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

### 7.1 The `high` check (finished, 11 November)

Run: `configs/experiments/flash_thinking_high.yaml` (3.5 Flash-Lite, `thinking_level: high` set
through `model_overrides`; same 3 tasks, `obs_mask`, B = 12,000; tagged `pilot`). It started
automatically when the re-pilot's Flash-Lite cells were done. `configs/models.yaml` still says
`minimal`.

| | 3.5 `minimal` (re-pilot) | 3.5 `high` |
|---|---|---|
| Solves | 0 of 3 | 0 of 3 |
| Terminations | submitted, step_cap, submitted | submitted, step_cap, step_cap |
| Steps | 48 / 75 / 63 | 53 / 75 / 75 |
| Source edited | attrs, tomlkit | attrs only |
| Requests used | 202 | 214 |
| Median latency per call | 1.9–6.1 s | 2.4–2.8 s |
| Thinking tokens per call (median / max) | 0 / 0 | 44–48 / 2,067–2,400 |
| Thinking tokens per episode | 0 | 15.5K / 14.8K / 18.7K |
| Output tokens per call (median) | 74–77 | 65–74 |
| `missing_reasoning` | 0 | 2 (of 203 calls) |
| Over-budget calls | 0 | 55 (17 / 17 / 21) |
| Server errors (5xx) | 16 | 11 |
| Elapsed per episode | 8.2 / 23.4 / 8.7 min | 4.9 / 10.6 / 7.0 min |

* **Latency and requests:** `high` costs no extra latency to speak of and about 6% more
  requests (214 against 202), because its episodes ran longer. Elapsed time differs mostly
  by how much 5xx waiting each run met, not by thinking.
* **Thinking:** most calls think very little (median 44–48 tokens); a few think 2,000+.
  That is "dynamic" as the docs describe it.
* **Signatures:** no signature problems. All 389 successful 3.5 attempts returned 200, and the
  only errors were 27 server 503s. No 4xx.
* **Reward:** 0 of 3, the same as `minimal`. Three episodes can't show a difference either
  way; I'm not claiming `high` helps or hurts.
* **The cost:** the over-budget calls (§9.2). `minimal` had none. That is a real difference
  between the two settings, and it isn't a model problem but a gap in our token estimate.
* **Quota:** 214 requests, so the day's 3.5 total is 416 of 450. It fit without a pause.
* **Recommendation:** put `high` into `configs/models.yaml` for 3.5 Flash-Lite **only after**
  the estimate fix in §9.2 is in, because otherwise every E3 episode would run over B.

## 8. Harness gaps (fixed, D16)

* **Retries never overwrite.** Each attempt is named `<cell>__a<n>` with a collision-safe
  suffix. Earlier attempts are listed in provenance, and `repogym status` reports attempts,
  cells retried and cells given up.
* **Heartbeat.** `repogym status` shows each worker's state, the running cell and attempt, its
  current step, minutes in, and seconds since the last model call. Verified on the re-pilot.
* **Unbounded `api_failure` retries** are replaced by the 3-attempt cap.

## 9. Re-pilot (finished: 0 of 6 solved)

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
* **Live confirmation of the quota hold: not obtained.** No run crossed the 08:00 UTC reset or
  ran out of daily quota, so no hold happened:
  * a WSL restart (boot at 23:27 UTC on 10 November) killed the scheduled jobs, so the three
    Flash-Lite runs started at 00:40 UTC on 11 November instead, about 7 hours before the
    reset;
  * they finished by about 01:50 UTC, with 416 of 450 (3.5) and 183 of 450 (3.1) requests
    used;
  * `daily_quota_pause` appears 0 times in the attempt logs, and no episode record has a
    `quota_pause` time.

  As you said, I haven't forced one. The hold stays covered by unit tests only (a call held
  20 hours across the reset continues in the same episode; the hold isn't active time or
  elapsed time) plus the sandbox's `sleep infinity` configuration. It will show up in a real
  run the first time an E1–E3 model reaches its daily limit, and I'll report it then.

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

### 9.2 Flash-Lite half (finished 11 November, about 01:20 UTC)

3.5 Flash-Lite at `minimal`, as in the original plan:

| Cell | Termination | Steps | Source edited | `str_replace` (failed) | `run_tests` / `submit` | Exact repeats | Over-budget | Retries |
|---|---|---|---|---|---|---|---|---|
| attrs-1448 | submitted, unsolved | 48 | `validators.py`, `.pyi`, a test file | 4 (0) | 6 / 1 | 10 | 0 | 6 |
| tomlkit-536 | step_cap | 75 | `container.py` | 1 (0) | 3 / 0 | 9 | 0 | 8 |
| click-3504 | submitted, unsolved | 63 | no | 0 | 10 / 1 | 15 | 0 | 2 |

* **Solves: 0 of 3.** With Gemma 26B's 0 of 3, **0 of 6** re-pilot episodes solved.
* **What the fixes did:** 0 `missing_reasoning` events; 0 budget violations; 0 summary events;
  masking-driven loops are gone (the top repeated call is `run_tests` on the same file after
  edits, 6 and 7 times, which is legitimate).
* **Behaviour:** the agent reads a lot and edits rarely. tomlkit-536: 56 `view_file` calls and
  one `str_replace` in 75 steps. click-3504: 30 views, 22 bash calls, 10 test runs and no
  edit at all; it submitted an empty diff at step 63.
* **attrs-1448:** it edited both files the gold patch touches, plus a test, and the hidden
  tests failed. I haven't diffed it against the gold patch; that comparison is useful to do
  before Gate 2.

**Why Flash-Lite and 3.1 went over B with thinking on.** 3.1 (42 calls) and 3.5 `high` (55)
went over, while 3.5 `minimal` and Gemma (after D21) didn't.

* In a typical over-budget call, our estimate was 11.9K (inside B), and the API billed
  12.3–14.6K. The worst gap was 30%.
* The gap tracks the thinking tokens of the earlier turns still in the prompt. Across the
  75-step episodes the correlation is 0.72–0.75, with a slope of about 0.6 billed tokens per
  kept thinking token. (The shorter episodes show weaker or no relationship.)
* **That points at the thought signatures** the provider replays with each function call.
  Gemini returns no thought text, but a signature carries the reasoning state, and it seems
  to be billed. I haven't confirmed that; the slope is a regression, not a measurement. The
  confirming test is cheap: replay the same history with and without the signatures and
  compare the billed prompt, as I did for Gemma (D21).
* The estimator counts the text and function-call parts but not signatures, so it
  underestimates whenever thinking is on.
* **Fix I propose** (small, same pattern as D21): add a `replay_tokens` estimate for Gemini,
  calibrated from this data and refreshed from each call's billed prompt, and count it before
  deciding what to keep. It needs a short confirming probe first. Without it, `high` runs
  over B in every long episode.

**Preserved-thinking share of the prompt (the analysis-plan item from D22),** for Gemini the
share is the kept thinking tokens times the slope above. It will be reported per strategy and
episode in RESULTS.md with an explicit "estimated" label for Gemini and "exact" for Gemma.

### 9.3 Stop rule, findings, and decisions needed

**The stop rule is reached:** 0 of 6 re-pilot episodes solved. Repetition has not persisted
(exact repeats are mostly test re-runs, and the post-mask loops are gone). Following your
instruction I haven't started Gate 2 work (no PREREGISTRATION.md, no freeze, no E1–E3).

**Where the 12 episodes stand** (the 6 re-pilot episodes, the 3 `high` episodes and the 3
episodes of 3.1; Gemma counted by final attempt):

| Run | Solved | Edited the gold file | Notes |
|---|---|---|---|
| Gemma 26B (re-pilot) | 0 / 3 | 1 | one `context_failure`; heavy 5xx |
| 3.5 `minimal` (re-pilot) | 0 / 3 | 2 | 0 violations |
| 3.5 `high` | 0 / 3 | 1 | 55 over-budget calls |
| 3.1 `high` | 0 / 3 | 3 | no reasoning text; 42 over-budget calls |

**What I think this says.** The harness works: it grades correctly, the agent follows its
tools, budgets hold for the two configurations without hidden thinking cost, and the failure
modes of the first pilot are gone. What's left is that these small models don't fix these
tasks within 75 steps. Seven of the 12 episodes edited the right file and still failed the
hidden tests. That is either a **floor effect** (the tasks are too hard for these models
at this budget) or a **tooling problem** I haven't found yet. Three tasks per model can't tell
those apart.

**Decisions I need from you** (none is implemented):

1. **Floor effect, how to check before E1.** Options:
   * **(a, recommended)** Diff the 7 right-file edits against the gold patches, and run the
     3.1 and 3.5 `high` configurations on 5 more dev tasks that the Oracle shows are small
     (fewest changed lines), before deciding. About 400 requests per model.
   * **(b)** Go ahead with E1 as planned and report a low solve rate as a finding. A floor
     effect would make the strategy comparison (RQ1) uninformative, so I don't recommend it
     without (a).
   * **(c)** Raise `max_steps` or B for the pilot. Both are pre-registered parameters, so
     that is a design change and needs your explicit say-so.
2. **Gemini estimate fix (§9.2).** Approve a confirming probe, then the `replay_tokens`
   estimate for Gemini. Needed before any `high` run.
3. **Flash-Lite `high` in `models.yaml`** (§7.1): yes after item 2, or stay at `minimal`.
4. **Replace 31B with 3.1 Flash-Lite in E3** (§6.2b), and what that does to the §6.3 rule.
5. **Gemma options A, B and C** (above).
6. **3.1's missing reasoning text.** Options: accept it (its thinking is in thought tokens),
   or require text and count a missing one as a `missing_reasoning` event only, which is what
   happens now. The reminders don't work. I recommend accepting it and reporting the rate.

## 10. Time estimates

These are based on pilot throughput. They will be revised with the re-pilot's steps per
episode.

| Experiment | Size | Binding limit | Estimate |
|---|---|---|---|
| E1 | 26B × 4 strategies × 25 dev tasks = 100 episodes | 26B TPM, ~1 call/min | ~3.5 days at ~50 calls per episode; ~6 days at 75 |
| E2 | winner × 10 tasks × 3 repeats = 30 episodes | 26B TPM | ~1 day |
| E3 | 3 models × 48 test tasks | Flash-Lite RPD: observed 54–83 requests per episode (mean ~70), so ~3,400 requests at 450/day | ~7.5 days per Flash-Lite model (3.5 and 3.1 each have their own quota, so in parallel), with 26B alongside |

## 11. Parameter summary

| Parameter | Value | Status |
|---|---|---|
| Agent prompt, view text, step counter, nudges, reasoning events | D19 | implemented |
| `view_file` max lines | 150 | implemented |
| Prompt sizing | estimator + Gemma's re-billed thinking; no `countTokens` | implemented (D20, D21) |
| `api_failure` attempts per cell | 3 | implemented |
| `max_wall_seconds` (active) / `max_elapsed_seconds` | 3,600 / 21,600 | unchanged |
| B, clip, M, K, summary length, `max_steps` | 12,000 / 2,000 / 5 / 4 / 1,000 / 75 | unchanged for the re-pilot |
| Flash-Lite `thinking_level` | `minimal` → proposed `high` | **needs your decision** (§7.1, §9.3) |
| E3 31B rule | §6.3 | to pre-register at Gate 2 |

I am stopping here for your approval. Decisions needed are in §9.3.
