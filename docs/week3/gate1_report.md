# Week 3: Gate 1 report

*Status: **interim**, written 2026-11-09 at about 13:00 UTC while the pilot is still running.
§3 will be finalised when the pilot ends. Everything else is final for Gate 1.*

## Summary

* **The build (Phase 1, §2.1–2.6) is complete and tested.** 383 unit tests pass (301 at the
  start of the week), plus 5 Docker integration tests and 9 manual live-API tests. CI is green on
  the pushed `week3` branch.
* **The harness changes don't affect grading or anti-cheating.** The Oracle and Cheater reruns on
  the 25 dev tasks met every expectation. A tool-agent episode has the same provenance, flags and
  grading path as an Oracle episode (Docker test).
* **The pilot shows a floor effect.** Every finished episode ended at the 75-step cap with **no
  edit to the source code and no `submit`**. The causes are identified (§5) and are mostly in
  our agent and context parameters, not in the models.
* **Gemma 4 31B is unreliable on the free tier.** Both its pilot episodes ended as `api_failure`.
  Gemma 4 26B is reliable but throughput-bound.
* **Proposals for approval:**
  * prompt and tool fixes (§5.3);
  * token accounting that includes Gemma's re-billed thinking (§4.3);
  * a bounded `api_failure` retry rule (§6);
  * a short second pilot before Gate 2.

## 1. What was built

| Area | What it is | Decisions | Tests |
|---|---|---|---|
| Providers | google-genai `generateContent`, native function calling, thought signatures replayed verbatim, SDK retries off; OpenAI-compatible provider; per-call tokens, latency, finish reason, HTTP status, attempt log | D8–D10, D12 | `test_providers.py` (15), live (9) |
| Rate limiting | per-model RPM, TPM and RPD at a 90% margin; persistent daily ledger reset at midnight Pacific; 429 and 5xx handling that tells per-minute from daily limits; exact limiter and backoff waits | D11 | `test_ratelimit.py` (13) |
| Context | `sliding_window`, `obs_mask`, `llm_summary`, `hybrid` under B; head+tail clip; online-calibrated estimator; optional exact counter; invariant property tests | D12, D14, D15, D18 | `test_context.py` (13) |
| Episode limits | 75 steps; the wall-clock cap counts **active time only**; 6 h elapsed safety cap, counted as `api_failure`; six termination categories | D17 | fake-clock tests in `test_tool_agent.py` |
| Agent | function-calling agent acting only through the environment API | D9 | `test_tool_agent.py` (18), Docker equivalence test |
| Runner | `repogym run --experiment`, one worker per model, resumable and idempotent, `repogym status`, full provenance, test-split guard | D16 | `test_experiments.py` (19), `test_cli_week3.py` (3) |
| Accounting | tokens (prompt, cached, output, thinking), requests, summary overhead, Flash-Lite list-price cost, budget violations, estimation error, waiting and API time, `summary_omitted` | D17, D18 | `test_tool_agent.py` |
| Logging | secret redaction on every log event | — | `test_logs.py` (4) |

* **Branch:** `week3`, 27 small commits, no AI attribution.
* **CI:** run `42606397627` passed (lint, formatting, unit tests). Its only warnings are about
  the runner image (the deprecated Node.js 20 actions and the move to Ubuntu 26).
* **Oracle and Cheater reruns** after the harness changes, on the 25 dev tasks at `ff3aad3` on a
  clean tree:
  * **Oracle:** 25/25 solved, no flags (`data/runs/week3-dev-oracle`).
  * **Cheater:** 0/25 solved, all 25 flagged (`data/runs/week3-dev-cheater`). The
    `stored_observation_truncated` flag (D14) now appears too, because the Week 2 agents keep the
    10,000-character cap.
* **Pilot hygiene:** the pilot is dev-only and tagged `pilot` in provenance. The guard was tested
  against the committed pilot file before the run: it is refused as soon as any test-split task
  is added.

## 2. Verified API facts

Checked 2026-11-09. Details and evidence files are in DECISIONS.md D8–D13 and
`docs/week3/evidence/`.

| Fact | Source | Evidence |
|---|---|---|
| TPM counts **input** tokens; quotas reset at **midnight Pacific**; limits apply **per project, per model** | https://aistudio.google.com/docs/rate-limits.md | D11 |
| `generateContent` is labelled "legacy" but supported; the Interactions API is the default | https://aistudio.google.com/llms.txt | D8 |
| Gemini 3 and 4 need thought signatures returned during function calling; only the current turn is validated | https://aistudio.google.com/docs/thought-signatures.md?generate-content=true | D12, live tests |
| Gemini 3.x: "strongly recommend not changing" temperature, top_p, top_k; Flash-Lite thinks at `minimal` by default | https://aistudio.google.com/docs/thinking.md?generate-content=true | D10, probes |
| Gemma 4: temperature 1.0, top_p 0.95, top_k 64; training-data cutoff Jan 2025 | https://ai.google.dev/gemma/docs/core/model_card_4 | D10 |
| Flash-Lite knowledge cutoff Mar 2026 | https://deepmind.google/models/model-cards/gemini-3-5-flash-lite/ | README |
| `countTokens` takes either `contents` or a full `generateContentRequest`, "never both" | https://aistudio.google.com/api-reference/tokens/count-tokens.md | D13 |

Measured:

* **Thinking settings.** gemma-4-26b thinks by default and accepts only `minimal` and `high`.
  Flash-Lite doesn't think by default.
* **Multi-turn tool calling** (all three models, live): thought signatures come back on function
  calls; a 3-step loop with signatures replayed works; a prompt with a dropped middle step plus
  a summary part is accepted (HTTP 200).
* **countTokens accuracy:** full-request `countTokens` matched the reported `prompt_tokens`
  within 1 token on single-turn prompts (519 against 520 on all three models).
* **New finding from the pilot: Gemma re-bills its earlier thinking (§4.3).**

## 3. Pilot results (interim)

Pilot design: 3 dev tasks, one from each of the three largest repos (pallets__click-3473,
pylint-dev__astroid-3057, PyCQA__isort-2683) × 3 models × `obs_mask`, B = 12,000, 1 repeat.
The run started at 11:06 UTC.

| Cell | Termination | Reward | Steps | Elapsed | Active | Waiting | Input tokens |
|---|---|---|---|---|---|---|---|
| flash-lite · click-3473 | step_cap | 0 | 75 | 5.9 min | 3.3 min | 2.6 min | 773,973 |
| flash-lite · astroid-3057 | step_cap | 0 | 75 | 13.0 min | 13.0 min | 0 | 608,911 |
| flash-lite · isort-2683 | step_cap | 0 | 75 | 8.5 min | 7.6 min | 0.9 min | 524,601 |
| gemma-26b · click-3473 | step_cap | 0 | 75 | 84.5 min | 14.4 min | 70.1 min | 755,664 |
| gemma-31b · click-3473 (attempt 1) | api_failure | 0 | 29 | 42.5 min | 28.3 min | 14.2 min | 175,029 |
| gemma-31b · click-3473 (attempt 2) | api_failure | 0 | 27 | 65.4 min | 32.6 min | 32.8 min | — |
| gemma-26b · astroid, isort | running | | | | | | |
| gemma-31b · astroid, isort | not started (see §6) | | | | | | |

* **Budget violations:** Flash-Lite 0 of 225 calls. Gemma 26B **21 of 75**, explained in §4.3.
  `summary_omitted` was 0, as expected, since `obs_mask` has no summary.
* **Estimation error:** mean +6.5% on Flash-Lite and +9.5% on Gemma 26B.
* **Flash-Lite requests:** 252 of the 450 per day allowed at the 90% margin. Its pilot cells are
  finished, so no more Flash-Lite requests are needed today.

## 4. countTokens decision (D13)

### 4.1 Evidence

* **The docs** don't say whether `countTokens` counts against limits.
* **RPM, measured:** 35 calls in 8.9 s on each Gemma model (limit 30 RPM), with no 429.
* **TPM, measured:** 30 full-request calls in 11.9 s on gemma-4-26b counted 1,166,700 tokens,
  about 73 times the 16K TPM, with no 429
  (`evidence/count_tokens_full_request_gemma-4-26b-a4b-it.json`).
* **RPD: still unknown.** It can only be seen on the AI Studio dashboard. **Operator action:**
  please check whether today's `countTokens` calls appear in the gemma-4-26b request count.
  About 130 were made today: the 75 pilot shadow counts plus the probes.

### 4.2 Proposal

* **Gemma:** use exact pre-call counts (`countTokens` plus the correction in §4.3), with the
  estimator as fallback. This assumes the dashboard confirms `countTokens` is RPD-free or cheap.
* **Flash-Lite:** keep the estimator. Its RPD (500) is scarce, and the pilot showed 0 violations
  and +6.5% mean error.

### 4.3 Gemma re-bills earlier thinking as input tokens

* **The finding.** For every one of Gemma 26B's 75 calls:

  `reported prompt_tokens − countTokens = (thinking tokens of earlier replies still in the prompt) + 1`

  The correlation is 1.0 with a slope of 1.0. When thought signatures are replayed, the API
  restores the earlier thoughts and bills them as input. `countTokens` doesn't count them, and
  neither does our estimator. That is why 21 calls exceeded B: by up to 41%, with a
  16,943-token prompt against B = 12,000. It also spends TPM we didn't plan for.
* **Fix (proposed; it changes code, so it waits for approval).**
  * The context manager adds the recorded `thinking_tokens` of every assistant message kept in
    the view to both the estimate and the exact count.
  * A live test will pin the "+ kept thinking" rule for 26B and 31B. Flash-Lite at `minimal`
    thinks 0 tokens, so it isn't affected.
* **Consequence for RQ1.** Under B, a thinking model's earlier thoughts compete with its
  observations for room in the prompt. This is reported as a property of the setup, and it is
  an argument for not raising Gemma's thinking level.

## 5. Floor-effect risk

### 5.1 Evidence on why episodes fail

Data: all 4 finished episodes plus the surviving 31B trajectory, read directly by
`scripts/diagnose_pilot.py` (read-only; `python3 scripts/diagnose_pilot.py [cell-substring]`).

| | flash · click | flash · astroid | flash · isort | 26B · click | 31B · click (att. 2) |
|---|---|---|---|---|---|
| Source edits | **0** | **0** | **0** | **0** (2 repro scripts) | 0 |
| First edit of any kind | — | — | — | step 30 (`repro.py`) | — |
| `run_tests` calls / `submit` | 0 / 0 | 1 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Tool mix | 71 view_file, 4 bash | 41 bash, 33 view, 1 tests | 40 view, 35 bash | 45 view, 28 bash, 2 create | 17 view, 12 bash |
| Exact repeated actions (first at step) | 12 (23) | 24 (27) | 21 (12) | 10 (18) | 1 |
| …of which the previous result was **masked** | 5 | **22** | **19** | 2 | 5 masked re-reads |
| Re-reads of a file after its earlier read was masked | 60 | 29 | 33 | 37 | 12 |
| Observations clipped (> ~4,700 chars) | **58/75** | 17/75 | 8/75 | 7/75 | 3/29 |
| Characters hidden by the clip | 48% | 86% | 23% | 26% | 46% |
| Tool errors | 3 view ranges > 400 lines | 2 bad paths, 1 import error | 2 reversed view ranges | 7 empty greps, 1 missing file | 1 |
| Replies with any reasoning text | **0/75** | **0/75** | **0/75** | **0/75** | 0/29 |
| Thinking tokens | 0 | 0 | 0 | 11,191 | 1,587 |
| Gold file found and read | yes (`core.py`) | **no** | yes (`core.py`) | yes | yes |

What the trajectories show:

1. **No planning is carried across steps (the main cause).**
   * Neither model writes any text with its tool calls. Flash-Lite at its default `minimal`
     thinking level produces 0 thinking tokens.
   * Assistant messages are the one part of history that is never masked (D12). With no text
     in them, nothing the agent learned survives once its observation is masked.
   * The Week 2 bash agent, which solved isort-2683 with Flash-Lite in 88 and 46 steps,
     *required* a `THOUGHT:` line before every command.
2. **The clip makes large reads useless (Flash-Lite on click).**
   * `view_file` allows up to 400 lines. Click's `core.py` windows are 9,000–17,000 characters,
     and the 2,000-token clip keeps about 4,700: the head and tail. The model never sees the
     middle of what it asked for.
   * So it asks again: 71 of 75 calls were `view_file` on `core.py`.
   * The first message also tells the model that outputs are "truncated to 200000 characters".
     That's the storage cap (D14), not what the model sees.
3. **Masking with M = 5 drives repetition (Flash-Lite on astroid and isort).**
   * 22 of 24 and 19 of 21 exact repeats came right after the previous identical result had
     been masked.
   * Example: `git grep -n "Self" astroid/` (2,345 characters, never clipped) was run 8 times,
     each time after the previous result was hidden.
   * isort-2683: the same `python3 -c` reproduction ran 8 times, alternating with re-reads of
     the same `core.py` ranges.
4. **Gemma 26B explores without converging.**
   * Its repeats are mostly while the result is still visible (8 of 10).
   * It wrote two reproduction scripts that crash, and ran several greps that return nothing.
5. **Tools aren't the bottleneck.**
   * There were no failed `str_replace` calls, because nobody tried one.
   * `run_tests` was called once, and its output wasn't clipped.
   * The tool errors (oversized or reversed view ranges, bad paths) were few and recovered from.
6. **Prompt clarity.**
   * How to finish is clear: "when you are done, call submit", plus the `submit` tool.
   * What is missing: when to stop exploring, the step budget (stated once and never
     repeated), and how the model's view is limited.
7. **Would more steps have helped? Very unlikely.**
   * No episode had made a source edit by step 75, and loops started between steps 12 and 27.
     A 100-step cap would add 25 more loop steps.
   * The Week 2 comparison doesn't support "more steps": that agent had the full linear history
     (2.18M prompt tokens in the 88-step solve), observations up to 10,000 characters, and
     required reasoning.

### 5.2 How much the floor matters

* **The Week 2 bash agent is the useful comparison.** With unbounded context and Flash-Lite it
  solved 2 of 7 episodes, 1 of 3 distinct dev tasks (isort-2683).
* **With 25 dev tasks and exact McNemar, RQ1 needs discordant pairs.** At a pass rate of about
  5%, the four strategies can't be told apart. Somewhere around 20% or more gives a chance of
  detecting large differences.

### 5.3 Proposed fixes

All of these are proposals, and none is implemented until you approve. They are listed by
expected impact.

1. **Prompt: require brief reasoning in text with every tool call.** "State in one or two
   sentences what you learned and what you'll do next."
   * This puts the agent's memory where masking can't reach it: our strategies hide old
     observations, but never edit assistant messages (D12).
   * It costs output tokens and, for Gemma, some TPM. It matches the Week 2 agent's design.
2. **Prompt: say what the agent can actually see.**
   * Results over ~2,000 tokens are cut in the middle; prefer `grep -n` and narrow ranges.
   * Results older than the last 5 tool calls are hidden; write down anything you'll need.
   * Replace the misleading "200000 characters" line (Week 3 agent only; the Week 2 prompt
     stays unchanged).
3. **Prompt: budget awareness.** Append `[step k/75]` to each tool result shown to the model,
   plus one nudge at step 60: make your edit, run the relevant test, and submit.
4. **Tool: `view_file` at most 150 lines per call** (now 400). A 150-line window is about 6,000
   characters, close to what the clip keeps, so a read shows what it promises.
5. **Context parameters for E1, to decide at this gate.**
   * (a) Keep M = 5, relying on fixes 1–2.
   * (b) Raise M. Before choosing a value I'll take the Complexity Trap's setting from the paper
     itself rather than from memory. A larger M under B = 12,000 makes the drop fallback fire
     more often.
   * **Recommendation:** (a) for the re-pilot. Change M only if the re-pilot still shows
     mask-driven repeats.
   * B stays at 12,000. With the §4.3 correction it is a true input-token bound, and Gemma's
     effective TPM (14,400) leaves little room above it.
6. **Step cap: keep 75** (the brief's value). The evidence says the cap isn't what binds.
   Revisit only if the re-pilot shows episodes editing late and running out.
7. **Re-pilot before Gate 2:**
   * 6 dev tasks (the 3 above plus 3 others, one per remaining repo) × Flash-Lite and Gemma 26B
     × `obs_mask`;
   * about 450 Flash-Lite requests, so it runs on its own day;
   * it is excluded from analysis like the first pilot.
   * **Go criterion for E1:** at least 1 solve, and most episodes making a source edit. If not,
     we come back with a decision between a larger context budget and secondary metrics (for
     example the fraction of fail-to-pass tests passing).

**My estimate:** with fixes 1–4, a dev pass rate of roughly **10–25%** for Flash-Lite and Gemma
26B, with low confidence. It's based on the Week 2 agent's 1 of 3 tasks with unbounded context,
and on the fact that today's failures are almost all agent-design failures rather than models
reaching the wrong fix.

That is enough for E2 and E3 to be meaningful, but marginal for RQ1's four-way comparison on
25 tasks. If the re-pilot lands below about 10%, I'd recommend telling you so before Gate 2
rather than running E1 into a floor.

## 6. Gemma 4 31B reliability, and the E1 workhorse

### 6.1 Gemma 31B's error record

| Attempt | Window (UTC) | Steps done | Successful calls | 500 | 503 | Ended by | Median / max API latency |
|---|---|---|---|---|---|---|---|
| 1 | 11:07–11:50 | 29 | 29 (25 retries) | (trajectory overwritten, see below) | | `ProviderUnavailable`: one call failed 6 attempts | — |
| 2 | 11:51–12:56 | 27 | 27 (36 retries) | 29 | 7 | `ProviderUnavailable`: one call failed 6 attempts | 44 s / 182 s |
| for comparison: 26B | 11:07–12:32 | 75 | 75 | 15 | 0 | step cap | 5 s / 49 s |
| for comparison: Flash-Lite | | 225 | 225 | 0 | 0 | step cap | 2–8 s / 21 s |

* In attempt 2, **57% of 31B's attempts returned a 5xx** (36 of 63). Two calls in 56 exhausted
  all 6 attempts, ending both episodes. Zero 429s: the limiter kept it inside its limits.
* The earlier probes saw 500 INTERNAL on every thinking variant. The live tests passed only
  because they are 3 calls long.

### 6.2 Recommendations

* **E1 workhorse: keep Gemma 4 26B.**
  * It is reliable enough: 17% of attempts were 5xx, all recovered by retries, and no episode
    failed.
  * It is fast per call (median 5 s).
  * It has ample RPD (14,400).
  * Its constraint is TPM. A full-context call (~12K input tokens, more with re-billed thinking)
    admits about 1 call per minute, so a 75-step episode takes about 85 min elapsed, 70 of them
    waiting.
* **Not 31B.** It failed 2 of 2 episodes, and its median latency is nearly 9 times 26B's.
* **Not Flash-Lite.** It is fastest per episode, but RPD-bound: 450 requests a day is about 6
  full episodes, so the 100-episode E1 would take about 17 days.
* **For E3:** keep 31B in the design only with a pre-registered reliability rule. Proposal:
  * a cell gets at most **3 `api_failure` episodes**, then it is excluded and reported;
  * a model with more than 20% excluded cells is reported as "unavailable on the free tier" and
    dropped from the RQ2 ranking.
  * Alternatively, replace 31B now, which is your call.

### 6.3 Harness issues the pilot exposed

To fix before Gate 2, with approval:

1. **`api_failure` retries are unbounded.** Except for the elapsed cap, an `api_failure` cell
   is retried forever with a 60 s pause. The pilot's 31B worker is now on its third attempt at
   click and will never reach astroid or isort if this keeps failing. **Decision needed:** once
   Gemma 26B finishes its 3 cells, may I stop the pilot (only the 31B worker would still be
   running) instead of letting it loop? Proposed fix: the 3-failure rule in §6.2.
2. **A rerun overwrites the cell's trajectory and diff.** Files are named by cell key, so
   31B's first attempt kept its record but lost its trajectory. Fix: suffix files with the
   attempt number.
3. **There's no in-progress view.** The run directory and records only appear when an episode
   finishes, so `repogym status` shows 0/3 for over an hour. Fix: a per-worker heartbeat file
   (cell, step, last call time), shown by `status`.
4. **Clip and mask events are re-emitted on every call.** They describe each prompt, which is
   correct, but it makes the raw event counts large (198 system steps in a 75-step episode).
   Analysis counts per call, not raw events; no fix needed, just documented.

## 7. Time estimates

These are based on pilot throughput. They assume a fixed agent averages about 50 calls per
episode, which still needs the re-pilot to confirm.

| Experiment | Size | Binding limit | Estimate |
|---|---|---|---|
| Re-pilot | 6 tasks × 2 models | Flash-Lite RPD (~450 requests) | 1 day |
| E1 | Gemma 26B × 4 strategies × 25 dev tasks = 100 episodes, ~5,000 calls | 26B TPM, ~1 call/min | **~3.5 days** (~6 days if episodes run to 75 steps); `llm_summary`/`hybrid` add summary calls |
| E2 | winner × 10 tasks × 3 repeats = 30 episodes | 26B TPM | ~1 day |
| E3 | 3 models × 48 test tasks | Flash-Lite RPD: 48 × ~50 = 2,400 requests at 450/day | **~5.5 days**; 26B runs in parallel (~2 days); 31B depends on reliability |

Runs go one worker per model, unattended, resuming across quota resets. Wall-clock total from
Gate 2 approval to E3 completion is about 11–12 days.

## 8. Proposed parameter changes (summary)

| Parameter | Now | Proposed | Why |
|---|---|---|---|
| Agent prompt | no reasoning, budget once, misleading limit | reasoning per call, view limits, `[step k/75]`, nudge at 60 | §5.1 (1), (2), (6) |
| `view_file` max lines | 400 | 150 | §5.1 (2) |
| Token accounting (Gemma) | estimate / `countTokens` | + kept thinking tokens | §4.3 |
| `token_counting` | shadow (Gemma), estimate (Flash-Lite) | exact (Gemma), estimate (Flash-Lite) | §4.2, pending the RPD check |
| `max_wall_seconds` (active) | 3,600 | 3,600 | longest active time so far is 33 min |
| `max_elapsed_seconds` | 21,600 | 21,600 | never bound; 26B used 85 min |
| `api_failure` retries per cell | unbounded | 3, then excluded and reported | §6.3 (1) |
| B, clip, M, K, summary length | 12,000 / 2,000 / 5 / 4 / 1,000 | unchanged for the re-pilot | §5.3 (5) |
| `max_steps` | 75 | 75 | §5.1 (7) |
| E3 31B rule | none | the reliability rule in §6.2, or replace | §6 |

**Gate 1 asks you to:**

1. approve or adjust the fixes in §5.3 and §8;
2. decide on stopping the pilot's 31B loop (§6.3 (1));
3. check `countTokens` against RPD on the dashboard (§4.1);
4. decide on 31B for E3 (§6.2).

Nothing further runs until you reply.
