# Week 3: Gate 1 report

*Written 2026-11-11 against the `week3` branch after the cleanup. It follows the five Gate 1
items of the original prompt. Everything in §5 is a **proposal and is not implemented**.
Nothing here is an experiment result for the study: the pilots were run on dev tasks only, are
tagged `pilot`, and are excluded from all analysis.*

**Decisions I need from you:** P1 and P2 (§5.1, §5.2), and each deviation in §5.3.

## 1. What was built, test counts, CI status

### 1.1 Built (Phase 1, sections 2.1–2.6, plus the pilot machinery of 2.7)

| Area | What | Files |
|---|---|---|
| 2.1 Providers | Native Gemini/Gemma provider (google-genai, `generateContent`, D8); OpenAI-compatible provider; one interface (D9); raw replies replayed verbatim so thought signatures are never edited (D12); vendor-default sampling and thinking, pinned (D10); per-call usage, latency, finish reason, HTTP status, retry count | `providers/{base,gemini,openai_compat,retry}.py`, `configs/models.yaml` |
| 2.2 Limits | Per-model limiter (RPM, TPM, RPD, 90% margin) from `configs/limits/gemini_free_tier.yaml`; persistent daily ledger keyed by the midnight-Pacific reset, safe across processes (file lock); 429/5xx retries (6 attempts, jittered backoff, server delay honoured); a call that hits the daily quota is held until the reset | `ratelimit.py`, `providers/retry.py` |
| 2.3 Context | `ContextManager` with `sliding_window`, `obs_mask`, `llm_summary`, `hybrid`; invariants; the common clip; token estimator calibrated online; budget-violation tracking | `context/{base,strategies,tokens}.py` |
| 2.4 Episode limits | Step cap 75; active-time cap plus a 6-hour elapsed safety cap; six termination reasons | `env/`, `harness.py` |
| 2.5 Runner | `repogym run --experiment`, one worker per model, resumable and idempotent, per-attempt files, provenance, `repogym status`, test-split guard | `experiments/{spec,runner,guard,status}.py`, `cli.py` |
| 2.6 Accounting | Per-episode tokens, requests, summary overhead, time, violations, estimation error; Flash-Lite list-price cost | `accounting.py`, `configs/prices.yaml` |
| New this round | Startup check: every configured model ID must be in the API's `models.list` (one call, no generation) or the run stops before any episode; `repogym check-models` saves the evidence | `experiments/models_check.py`, `docs/week3/evidence/models_list_2026-11-11.json` |
| Docs | Research framing, DECISIONS D8–D22, RUNBOOK, ARCHIVE | `docs/week3/`, `DECISIONS.md` |

### 1.2 Test counts

| Suite | Count | Notes |
|---|---|---|
| Unit tests (default `pytest`) | **400** passing | 301 are Week 2's; 99 are Week 3 (context 12, providers 19, rate limit 14, tool agent 17, experiments 25, logs 4, CLI 3, model check 5) |
| Week 2's 301 tests against the current code | **301 / 301** | run from a `week2-complete` worktree |
| Docker integration (`pytest -m docker`) | **5** passing | includes a tool-agent episode with the same provenance, flags and grading as an Oracle one |
| Live API tests (`pytest -m live`, manual, never in CI) | **6** | 2 checks × 3 models; last run 2026-11-09 |
| Hypothesis property test | 1, 150 examples | every strategy respects B, the pinned prefix, the last turn and call/response pairs |

**Week 1–2 behaviour is untouched.** Oracle (25/25, reward 1), Null (0/25) and Cheater (0/25, all
flagged) on the 25 dev tasks are identical to `week2-complete` in reward, steps, grading and
flags, after the cleanup.

### 1.3 CI

Green on the latest push (GitHub Actions `tests`, run 42974453092 on the model-check commit).
The workflow runs `ruff check`, `ruff format --check` and the unit tests; Docker and live tests
are not in CI.

## 2. Pilot results per model

This is the **first pilot**, which used the agent that is the current agent after the cleanup
(its post-pilot fixes are archived; see §5.1 and Appendix A). Design, per the original 2.7: 3
dev tasks (click-3473, astroid-3057, isort-2683) × 3 models, `obs_mask`, B = 12,000, 75-step
cap, 1 repeat. It started 11:06 UTC on 2026-11-09 and was **stopped at 13:03 UTC by decision**:
every finished episode showed the same failure (§5.1) and 31B was retrying one task without
progress. So **6 episode records exist, not 9 cells**: 26B astroid and 31B attempt 3 were in
flight and abandoned; 26B isort and 31B astroid/isort never started.

| Model | Task | Termination | Reward | Steps | Requests | Input / cached / output / thinking tokens | Elapsed (active + waiting) | 429s | Over-B calls | Estimator error (mean, worst under) |
|---|---|---|---|---|---|---|---|---|---|---|
| gemini-3.5-flash-lite | click-3473 | step_cap | 0 | 75 | 75 | 773,973 / 37,712 / 2,912 / 0 | 6.0 min (3.3 + 2.6) | 0 | 0 | +6.5%, −2.5% |
| gemini-3.5-flash-lite | astroid-3057 | step_cap | 0 | 75 | 75 | 608,911 / 152,897 / 2,547 / 0 | 13.3 min (13.0 + 0) | 0 | 0 | +6.6%, −2.4% |
| gemini-3.5-flash-lite | isort-2683 | step_cap | 0 | 75 | 75 | 524,601 / 223,256 / 4,702 / 0 | 8.7 min (7.6 + 0.9) | 0 | 0 | +6.7%, −5.4% |
| gemma-4-26b-a4b-it | click-3473 | step_cap | 0 | 75 | 90 | 755,664 / 41,646 / 2,854 / 11,191 | 84.7 min (14.4 + 70.1) | 0 | **21** | +9.5%, **−29.6%** |
| gemma-4-31b-it | click-3473 (att. 1) | **api_failure** (5xx) | 0 | 29 | 54 | 175,029 / 45,322 / 973 / 1,587 | 42.7 min | 0 | 0 | +3.1%, −8.0% |
| gemma-4-31b-it | click-3473 (att. 2) | **api_failure** (5xx) | 0 | 27 | 63 | 234,131 / 94,646 / 963 / 3,849 | 65.6 min | 0 | 0 | −0.3%, −24.5% |

* **Rewards: 0 of 6.** The four episodes that ran to the cap all hit 75 steps without editing
  any source file (§5.1).
* **Cost (Flash-Lite list-price equivalent, nothing billed):** $0.23, $0.15 and $0.11 per
  episode, from `configs/prices.yaml`. No cost is reported for Gemma.
* **Limiter target (zero hard 429 failures): met.** 0 429s in the pilot. The 5xx errors were
  server-side (§3.3).
* **Ledger accuracy against the dashboard:** Flash-Lite matched exactly (247 requests, after
  correcting for 18 blocked and 13 un-limited calls). The Gemma ledgers were within 3 and 8
  requests of the dashboard read, consistent with it being read 5–10 minutes earlier (§3.3).
* **Token-estimate error:** the estimator over-counts Flash-Lite by about 6.5% on average and
  never under-counted it by more than 5.4%. For Gemma 26B it under-counted by up to 29.6%,
  because Gemma bills its earlier thinking twice (D21). That is why `replay_tokens` is in the
  kept code (§5.3, deviation 3); a later run with it had 0 over-B calls on 26B (archived data).
* **Budget compliance:** Flash-Lite 0 of 225 calls over B; 26B 21 of 75 (pre-fix); 31B 0.
* **Real cost in tokens and requests per episode:** about 0.5–0.8M input tokens and 3–5K output
  tokens per 75-step Flash-Lite episode, and **75 requests** (one per step; `obs_mask` makes no
  summary calls).
* **Caveat.** 31B attempt 1's trajectory file was overwritten by attempt 2 in this pilot (a bug
  since fixed with per-attempt files, D16), so its numbers come from the episode record. The
  "active" and "waiting" split is as recorded at the time.

## 3. Verified API facts

Docs checked 2026-11-09; model IDs re-checked 2026-11-11. Details in DECISIONS D8–D12.

### 3.1 Documentation

| Fact | Source |
|---|---|
| TPM counts **input** tokens, so B = 12,000 is below Gemma's effective 14,400 (90% of 16K); limits apply per project and per model | https://aistudio.google.com/docs/rate-limits.md |
| Daily quotas reset at **midnight Pacific time** (08:00 UTC since 1 November, 07:00 UTC in summer) | same page |
| `generateContent` is "legacy" but supported; we use it statelessly (D8) | https://aistudio.google.com/llms.txt |
| Thought signatures: the first `functionCall` of each step in the current turn must carry its signature; documented dummy signatures skip validation; never edit assistant parts, drop or summarise only whole steps (D12) | https://aistudio.google.com/docs/thought-signatures.md?generate-content=true |
| Gemini 3.x: don't change the sampling defaults; Flash-Lite thinking levels are `minimal` (default), `low`, `medium`, `high`; "return the entire response with all parts" | https://aistudio.google.com/docs/thinking.md?generate-content=true |
| Gemma 4: temperature 1.0, top_p 0.95, top_k 64; "thinking content should be preserved" in tool-call turns | https://ai.google.dev/gemma/docs/core/model_card_4 |
| Flash-Lite list price: $0.30 input, $2.50 output including thinking, $0.03 cached input (per 1M tokens) | https://aistudio.google.com/docs/pricing.md |

### 3.2 Model IDs, limits, sampling, cutoffs

IDs were verified against `models.list` on **2026-11-11** (62 models listed); the entries for the
three are saved in `docs/week3/evidence/models_list_2026-11-11.json`. Every run repeats this
check at start.

| Model ID | Version (`models.list`) | Free-tier limits (operator dashboard, 2026-11-09) | Sampling and thinking (pinned, D10) | Knowledge cutoff |
|---|---|---|---|---|
| `gemini-3.5-flash-lite` | 3.5-flash-lite-07-2026 | 15 RPM · 250K TPM · 500 RPD | API defaults; `thinking_level: minimal` (the documented default) | March 2026, some domains January 2025 ([model card](https://deepmind.google/models/model-cards/gemini-3-5-flash-lite/)) |
| `gemma-4-31b-it` | 001 | 30 RPM · 16K TPM · 14.4K RPD | 1.0 / 0.95 / 64; thinking at the API default | training data to January 2025 ([model card](https://ai.google.dev/gemma/docs/core/model_card_4)) |
| `gemma-4-26b-a4b-it` | 001 | 30 RPM · 16K TPM · 14.4K RPD | 1.0 / 0.95 / 64; thinking at the API default | same |

### 3.3 Measured

* Multi-turn tool calling with signatures replayed verbatim, and a history with a dropped middle
  step plus a summary part, work on all three models
  (`docs/week3/evidence/live_tool_calling_2026-11-09.md`; API probes in
  `docs/week3/evidence/api_probes_*.json`).
* **Gemma returns its reasoning as a `thought: true` text part, billed at twice its thinking
  tokens when replayed (D21).** This is why its prompts ran over B in the pilot.
* **Dashboard cross-check (RPD).**

  | Model | Dashboard | Ledger | Adjustment | Expected | Difference |
  |---|---|---|---|---|---|
  | Flash-Lite | 247 | 252 | −18 blocked by the unit-test network guard, +13 sent outside the limiter | 247 | **0** |
  | Gemma 26B | 168 | 184 | −18, +10 | 176 | −8 |
  | Gemma 31B | 159 | 172 | −18, +8 | 162 | −3 |

  The Gemma differences fit a dashboard read 5–10 minutes before the ledger, while each model
  was sending about one request a minute.
* **Dashboard TPM peaks:** 19.65K (26B) and 18.34K (31B), above 16K. Each 26B overrun was a
  single call whose billed size passed B and 16K because re-billed thinking was not in the
  estimate, and the limiter admitted on that estimate. The 31B 5xx errors did **not** coincide
  with over-limit minutes (0 of 17 errored calls came in a minute already over the limit), so
  they are server-side.
* **Server-error rates (attempts that returned 5xx), 9–11 November,** from the per-attempt data
  in the archive: Flash-Lite 27 of 641 (4%), Gemma 26B 155 of 419 (37%; 14–47% by hour), Gemma
  31B 60 of 95 (63%). A dedicated 31B probe (20 spaced single calls over 12 hours) succeeded 8
  times. Evidence and method are in the archive tag (Appendix A).
* **A quota hold across the 08:00 UTC reset has not been observed live.** It is covered by a
  fake-clock unit test only
  (`test_daily_quota_holds_the_call_until_the_reset_then_continues`).

## 4. Throughput-based time estimates

**Assumptions.** The current agent (the first-pilot agent) and the original retry policy: up to 6
attempts per call with jittered backoff (2 s doubling, about a minute in total), then the
episode ends as `api_failure`; a cell gets at most 3 `api_failure` attempts (D16).

**Per-episode throughput (from the pilot).**

* **Flash-Lite:** 6–13 minutes of compute per 75-step episode and **75 requests**. Its limit is
  the **500 RPD** (450 at the 90% margin), about 6 full episodes a day.
* **Gemma (either model):** TPM-bound. 75 steps at B = 12,000 against 14,400 effective TPM is at
  least 62 minutes; the pilot's 26B episode took **84.7 minutes**, including 70 minutes waiting
  on the limiter and 5xx backoff. I use **55–85 minutes** per episode (50 to 75 steps).
* Every capped pilot episode used all 75 steps, so the upper figures are the likely ones while
  the agent is unchanged.

**Effect of server errors under the 6-attempt policy.** A call fails after 6 failed attempts in a
row, so with per-attempt error rate *p* the chance that a 75-call episode ends as `api_failure`
is `1 − (1 − p⁶)⁷⁵` if errors were independent. They are bursty in practice, which only makes
long outages worse.

| Model, error rate *p* | Episode ends as `api_failure` | Expected attempts per cell (max 3) | Cells given up after 3 attempts |
|---|---|---|---|
| Flash-Lite, 4% | about 0% | 1.0 | 0% |
| 26B at 14% (best hours) | 0.06% | 1.0 | 0% |
| 26B at 37% (overall) | 17.5% | 1.2 | 0.5% |
| 26B at 47% (worst hours) | 55.6% | 1.9 | 17% |
| **31B at 63%** | **99.2%** | **about 3** | **about 98%** |

For 31B this matches the pilot: both attempts failed after 27 and 29 steps, close to the 16
calls the 6-attempt rule predicts (1 / 0.0625).

**Estimates** (wall-clock; models within an experiment run in parallel):

| Experiment | Size | Binding limit | Estimate |
|---|---|---|---|
| **E1** | 26B × 4 strategies × 25 dev tasks = 100 episodes | 26B TPM | 100 × 55–85 min = 92–142 h clean, **3.8–5.9 days**; with 26B's 5xx effect (+10% at 37%, +45% at 47%) **4–8.5 days**. `llm_summary` and `hybrid` add summary calls (+10–20% requests on half the episodes) |
| **E2** | winner × 10 tasks × 3 repeats = 30 episodes | 26B TPM | 27–43 h clean, **1.1–1.8 days**; **1.2–2.6 days** with 5xx |
| **E3, Flash-Lite** | 48 test tasks | **RPD:** 48 × 75 = 3,600 requests, plus about 4% for failed attempts, at 450 a day | **8–8.5 days** (about 8 hours of compute spread over those days); 9–10 days if the frozen strategy is a summarising one |
| **E3, Gemma 26B** | 48 test tasks | TPM | 44–68 h clean, **1.8–2.8 days**; **2–4 days** with 5xx |
| **E3, Gemma 31B** | 48 test tasks | **reliability**, not quota | clean: 1.8–2.8 days. At 63% errors about 98% of cells end as `api_failure` after 3 attempts of 40–65 minutes each (as in the pilot), so **4–6 days spent producing almost no usable episodes** |
| **E3 total** | three models in parallel | Flash-Lite RPD | **about 8–8.5 days** (Flash-Lite is the longest) |

E1 → E2 → E3 run in order (E2 needs E1's winner; E3 needs the frozen strategy), so Phase 2 is
**about 13–20 days** of runtime.

**Expected effect of 31B on E3.** At its measured error rate, 31B would end nearly every test
cell as `api_failure`. The pre-registered rule planned for the E3 exclusions (more than 20% of a
model's test cells ending `api_failure` after 3 attempts means "incomplete, excluded from
ranking", partial results in an appendix) would apply to 31B, and RQ2 would in effect compare
two models. The rule belongs in the pre-registration (Gate 2); it is not a change proposed here.

## 5. Proposed changes

### 5.1 Proposal P1: restore the Gate 1 agent fixes (not implemented)

**What.** Re-add the changes that the cleanup archived, with one difference: the 150-line
`view_file` limit lives **inside the tool agent**, so Week 2's 400-line `view_file` default
(`tools/file_ops.py`) and its tests stay untouched.

1. **Reasoning text required with each tool call.** A reply without it still runs, is recorded
   as a `missing_reasoning` event, and every second one in a row adds one short reminder.
2. **A per-strategy visibility text** in the system prompt: the clip size, masking after the
   last 5 results, oldest-first removal, the summary. It also replaces the misleading
   storage-cap statement by stating what the model is shown. Week 2's `environment.py` first
   message stays unchanged.
3. **`[step k/75]`** after each result, with one nudge at 15 steps left.
4. **`view_file` capped at 150 lines inside the agent:** the agent shows the model a tool
   description with the 150-line limit and clamps the `start_line`/`end_line` it sends to
   `env.step`, never touching the environment's own limit. It still acts only through the
   environment API.

**Why (evidence).** First pilot against a later re-pilot with these fixes (archived data in
`data/archive/pilots/`, diagnosis in the archive tag):

| | First pilot (current agent) | Re-pilot (with the fixes) |
|---|---|---|
| Episode records | 6 (4 reached the cap) | 9 (Gemma 26B ×6 attempts, Flash-Lite ×3) |
| Tool-calling replies with reasoning text | **0 of 354** | **367 of 367** |
| Episodes that edited a file the gold patch changes | **0 of 6** | **4 of 9** |
| Episodes that used `str_replace` | 0 of 6 | 4 of 9 |
| Exact repeats right after the earlier result was masked | 19–22 per capped Flash-Lite episode | 1–2 |
| Observations clipped | up to 58 of 75 (86% of characters hidden on astroid) | much less (150-line views fit the clip) |
| Solves | 0 of 6 | 0 of 9 |

* **Why the first pilot failed** (diagnosis of all four capped episodes):
  * No planning carried across steps. Assistant text is the one part of history that masking
    never removes, and the models wrote none (the Week 2 bash agent, which required `THOUGHT:`,
    solved isort-2683 in 88 and 46 steps).
  * 400-line windows of long files are 9,000–17,000 characters but the clip keeps about 4,700,
    so most reads were useless; 71 of 75 calls on click were `view_file`.
  * Masking with M = 5 drove repetition: 22 of 24 and 19 of 21 repeats on astroid and isort
    came right after the earlier identical result was masked.
  * The model could not see its step budget or what was hidden from it.
* **Limits of this evidence.** The two pilots used different tasks (they share only the click
  repo), n is 6 against 9, and **neither produced a solve**. P1 restores the behaviour the study
  needs in order to measure anything (an agent that edits and tests), not a pass rate. In the
  re-pilot 4 of 9 episodes touched a gold file and none passed the hidden tests (§5.2).
* **Cost and risk.** Small: prompt text, a clamp in the agent, a few events. It changes what the
  model sees, so it must be settled before any E1 run and frozen with the other prompts.
* **If P1 is not approved,** the first-pilot agent is the baseline for E1 and I expect a floor
  effect (episodes capped without edits), which would make RQ1 uninformative.

### 5.2 Proposal P2: two secondary metrics from the existing grading output (not implemented)

**What.** Two secondary metrics computed from data each episode already stores (the grade
record and the agent's diff). No extra runs, no harness change. They would be added to the
analysis plan; **pass@1 stays the primary metric and the strategy-selection rule is unchanged**.

1. **Gold-file localisation:** the share of the non-test files changed by the gold patch that the
   agent's final diff also changes (and "touched at least one" as a binary).
2. **FAIL_TO_PASS fraction:** `fail_to_pass_passed / fail_to_pass_total` from the grade, with the
   count of `pass_to_pass` regressions reported beside it.

**Why (evidence: 0 solves in all pilots).** All 21 graded episode records in the four pilots
(first pilot 6, re-pilot 9, a 3.1 Flash-Lite pilot 3 and a Flash-Lite `high`-thinking check 3;
all in `data/archive/pilots/`) scored reward 0, so pass@1 alone can't separate them. The same
records differ a lot:

| | Records | Touched a gold file | Touched **all** gold files | Passed some FAIL_TO_PASS tests |
|---|---|---|---|---|
| First pilot | 6 | 0 | 0 | 0 |
| Re-pilot | 9 | 4 | 2 | 4 (attrs-1448 3/4 ×3, tomlkit-536 1/1 ×1) |
| 3.1 Flash-Lite pilot | 3 | 3 | 2 | 2 (attrs 3/4, tomlkit 1/1) |
| 3.5 `high` check | 3 | 1 | 1 | 1 (attrs 3/4) |

* **A binary reward hides this.** tomlkit-536 passed its one FAIL_TO_PASS test in two records,
  but each broke 3–4 of 56 existing tests, so reward is 0. attrs-1448 reached 3 of 4
  FAIL_TO_PASS tests in five records across the pilots.
* **Use.** Reported beside pass@1 with the same intervals; never used to pick the winning
  strategy.
* **Caveat.** "Gold file" depends on how scratch files and tests are excluded from the agent's
  diff (I used non-test, non-scratch paths); the exact definition would be fixed in the
  pre-registration. It is a diagnostic of how close episodes get, not a proxy for solving.

### 5.3 Deviations from the original prompt that remain in the kept code

Each is implemented, tested and recorded. I'm asking for explicit approval of each.

| # | Deviation | Prompt says | Reason and evidence | DECISIONS |
|---|---|---|---|---|
| 1 | **Daily-quota hold in place.** A call that hits the daily quota waits for the reset (plus 60 s) and the episode continues, keeping its sandbox. The runner also sleeps until the reset for episodes that ended on the elapsed cap | "that model's worker sleeps until reset" | Ending the episode would throw away up to an hour of work and a sandbox. The hold is excluded from active time. Tested with a fake clock only; **never seen live** | D16 |
| 2 | **Active-time cap, plus a 6-hour elapsed safety cap.** The 3,600 s cap counts time the agent is not waiting on the limiter or a retry backoff; the elapsed cap (21,600 s) catches hangs and counts as `api_failure` | "a per-episode wall-clock cap" | Section 2.2 requires sleeping and backoff. A plain wall-clock cap would end episodes for waits the prompt itself requires. In the pilot the waits were up to 70 of 85 minutes per episode | D17 |
| 3 | **`replay_tokens` for Gemma.** Gemma's earlier thinking is kept in the history, as its model card requires for tool-call turns, and counted exactly (2× its thinking tokens) in the prompt estimate | 2.3: every prompt within B | Without it 26B ran over B in 21 of 75 calls (up to 29.6% under-estimate) and the limiter under-reserved TPM; with it a later run had 0 over-B calls. Kept thinking can take 40–71% of a long prompt, leaving less room for observations | D21 |
| 4 | **`MAX_API_FAILURES = 3` per cell.** After 3 `api_failure` attempts a cell is recorded and not retried. Daily-quota interruptions don't count | "api_failure episodes are retried after quota reset" (no cap) | Without a cap a persistently failing model (31B) retries forever and a run can't finish. **It must be stated in the pre-registration's exclusion rules** | D16 |
| 5 | **Per-attempt trajectory and diff files, never overwritten** (`<cell>__a<n>`) | idempotent runner | Overwriting lost 31B's first attempt in the pilot. It changes Week 2 file naming only when a file already exists; verified by the Oracle, Null and Cheater reruns | D16 |
| 6 | **Ledger file lock** and **safe reads of episode records during appends** | persistent ledger; parallel workers | The first ledger lost counts across processes (a 4-process test failed on it); a reader hit a half-written line | D16, D20 |
| 7 | **Stored-observation cap above the prompt clip.** Experiments must set `budgets.max_observation_chars` above the clip (the loader refuses otherwise), so stored observations aren't cut by Week 2's 10,000-character cap | a clip common to all strategies | The clip is prompt-only so that analysis can see both what the tool returned and what the model saw | D14 |
| 8 | **A summary that alone pushes the minimal prompt over B ends the episode as `context_failure`.** | 2.3 invariants | Found by Hypothesis (1 token over at B = 3,000). The earlier fallback was archived as beyond the prompt, so this edge case is an outcome, pinned in `tests/test_context.py`. A known limitation of `llm_summary` and `hybrid`, not expected at B = 12,000 | D18 (archived note) |

**Carried to Gate 2 (not changes to approve now):** state `MAX_API_FAILURES = 3` and the 31B
reliability rule in `PREREGISTRATION.md`.

**Other proposed parameter changes (original item 5): none.** I propose no change to B (12,000),
M (5), K (4), the clip (2,000 tokens), the summary length (1,000) or the step cap (75). More
steps wouldn't have helped the first pilot: its loops started between steps 12 and 27, and the
one Gemma episode in the re-pilot that edited and submitted did so at step 41.

## Appendix A: history of archived work

After the first version of this report the scope was reset to the original prompt. These items
were built and run, then **archived, not deleted**. They are described in
[ARCHIVE.md](ARCHIVE.md) and kept in full at tag `week3-archive-2026-11-11` (branch
`archive/week3-post-gate1`):

* the D19 agent fixes (the basis of P1), a 3.1 Flash-Lite pilot, a Flash-Lite `high`-thinking
  check, the 31B reliability probe and the error-by-hour report, the 5xx in-place retry policy,
  `countTokens`, `summary_omitted`, preserved-thinking reporting, and the heartbeat and process
  tooling;
* the earlier, longer version of this report (restore with
  `git show week3-archive-2026-11-11:docs/week3/gate1_report.md`);
* pilot data in `data/archive/pilots/` and pilot configs in `configs/archive/`. All analysis
  excludes everything under `archive/`.
