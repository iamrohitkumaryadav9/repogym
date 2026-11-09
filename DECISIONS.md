# Decision log

Numbered, non-obvious design decisions. Each entry gives its context, the options considered,
the decision, and its consequences. API facts cite the official documentation with the date it
was checked. Where an empirical check exists, its evidence file is named too.

**Tags.** Week 1 (the v0.1 dataset) is tagged `v0.1` and Week 2 is tagged `week2-complete`. There
is no separate `week1` tag, and we don't add one on an old commit.

## D1–D7 (Week 2), index

The full text of these decisions, their review outcomes and their evidence are in
[docs/design/week2_environment.md](docs/design/week2_environment.md).

| ID | Decision | Section |
|---|---|---|
| D1 | The MCP server runs on the host. Tools run in the sandbox via `docker exec` as the non-root `agent` user, and file tools refuse paths outside `/work` | §10, §12, §13 |
| D2 | Grading runs in a fresh container with validation's runner and test-file selection. The diff is extracted on the host, never through the agent's git | §5, §12, §13 |
| D3 | The revert scope covers test_patch paths, test files, `conftest.py`, pytest config, `sitecustomize`/`.pth`, and modules in import roots that shadow imports | §12, §13 |
| D4 | RepoGym has its own provider layer rather than LiteLLM | §9, §12 |
| D5 | Trajectories are ATIF v1.8, checked by our own validator and cross-checked with Harbor's | §7, §13 |
| D6 | The probe catches future information; past-history overlap is recorded, not failed on | §14 |
| D7 | Writable root with a disk budget; the read-only root was evaluated and rejected | §13, §14 |
| (OpenEnv) | Dependency-free adapter mirroring openenv-core 0.3.0 | §15 |

---

## D8. Use stateless `generateContent` through the official SDK, not the Interactions API

*2026-11-09.* Docs: <https://aistudio.google.com/llms.txt> (the index says "Use the Interactions
API … as the default … `generateContent` is still supported … but is considered a legacy API")
and <https://aistudio.google.com/docs/thought-signatures.md?generate-content=true>.

* **Context.** RQ1 compares context-management strategies. The strategy must decide exactly
  which tokens the model sees on every call.
* **Options.**
  * (a) The Interactions API in stateful mode. The server manages the context, which defeats
    the experiment.
  * (b) The Interactions API in stateless mode.
  * (c) `generateContent` via the `google-genai` SDK. It's stateless, we send the full
    `contents` each call, and the docs on thought signatures, function calling and usage
    metadata are complete for it.
  * (d) Keep the hand-written httpx client from Week 1.
* **Decision.** (c). `google-genai==2.33.0` (2026-10-23), with SDK retries off
  (`HttpRetryOptions(attempts=1)`) and automatic function calling off, so the RepoGym limiter
  and agent loop own both. The Week 1 classifier keeps its httpx client, unchanged.
* **Consequences.**
  * `generateContent` is labelled legacy; if Google removes it, the provider needs porting.
    The provider interface (D9) keeps that change in one module.

## D9. One tool-calling provider interface; models are configuration

* **Context.** The study needs three models through Gemini now, and other models later (Ollama
  or OpenAI-compatible) without new agent code.
* **Decision.** `repogym.providers.base.ToolProvider`:
  * `generate(system, messages, tools) -> Generation` returns the assistant message and a
    `Usage` record (prompt, cached, output, thinking and total tokens; latency; finish reason;
    HTTP status; retries; model version).
  * `count_tokens(...)` is optional.
  * Messages are provider-neutral, but an assistant message keeps the provider's native content
    (`raw`) and is replayed verbatim, so thought signatures can't be lost or altered by
    re-serialisation.
  * `GeminiProvider` (SDK) and `OpenAICompatProvider` (tool calling) both implement it.
* **Consequences.** A new model is a config entry in `configs/models.yaml`. Context strategies
  never edit assistant messages (D12).

## D10. Sampling and thinking: each vendor's documented defaults, pinned and recorded

*2026-11-09.* Sources:
* Gemini 3.x guidance in <https://aistudio.google.com/llms-full.txt>, "Parameter updates and best
  practices in Gemini 3.x": "**`temperature`, `top_p`, `top_k`**: we strongly recommend not
  changing the default values".
* The thinking levels table in <https://aistudio.google.com/docs/thinking.md?generate-content=true>,
  where Flash-Lite's default is `minimal`.
* The Gemma 4 model card <https://ai.google.dev/gemma/docs/core/model_card_4>, last updated
  2026-07-30: "temperature=1.0 top_p=0.95 top_k=64".

Measured on 2026-11-09 in `docs/week3/evidence/api_probes_*.json`:

| Model | Sampling sent | Thinking sent | Observed |
|---|---|---|---|
| gemini-3.5-flash-lite | none (API defaults, per the docs) | `thinking_level="minimal"` (the documented default, pinned) | no thinking tokens; `low` and `high` are accepted; `thinking_budget=0` gives a 400 |
| gemma-4-26b-a4b-it | `temperature=1.0, top_p=0.95, top_k=64` (model card) | none (API default) | thinks by default (209 thought tokens on a trivial prompt); accepts `minimal` and `high`; `low` and any `thinking_budget` give a 400 |
| gemma-4-31b-it | as for gemma-4-26b-a4b-it | none (API default) | every thinking probe returned HTTP 500 INTERNAL on 2026-11-09, so it's re-probed before the pilot |

* **Options considered.**
  * Force temperature 0 for every model. Rejected: the docs advise against changing Gemini 3.x
    sampling, and Week 1 showed temperature 0 isn't deterministic on Gemini anyway.
  * One common setting for all models. Rejected: it isn't any vendor's recommendation.
* **Decision.** As in the table. Every setting is pinned in `configs/models.yaml` and recorded in
  every episode's provenance.
* **Consequences.** Sampling is stochastic, so run-to-run variance is measured (E2), not assumed
  away. Thinking tokens are recorded per call and count as output tokens.

## D11. Rate limits: TPM counts input tokens, quotas reset at midnight Pacific, limits are per project and model

*2026-11-09.* Source: <https://aistudio.google.com/docs/rate-limits.md> (redirected from
ai.google.dev/gemini-api/docs/rate-limits), which says:
* "Tokens per minute (input) (**TPM**)";
* "quotas reset at midnight Pacific time";
* "Rate limits are applied per project, not per API key";
* limits vary by model.

The page doesn't say whether the minute windows are rolling or fixed.

* **Decision.**
  * The limiter enforces RPM, TPM (input tokens) and RPD per model, from
    `configs/limits/gemini_free_tier.yaml` (the operator's AI Studio dashboard values, with the
    date recorded), at 90% of each limit by default.
  * Minute limits use a **rolling** 60-second window. That's the conservative choice, since it
    never exceeds a fixed-window limit either.
  * The daily ledger is keyed on the Pacific calendar date (`America/Los_Angeles`, so DST is
    handled), and persists on disk.
* **Consequences.**
  * Because TPM counts input only, the per-prompt budget **B = 12,000** stays below Gemma's
    16K TPM with margin (90% of 16K is 14.4K). Output and thinking tokens don't count toward
    TPM.
  * With 12K-token prompts late in an episode, Gemma is limited to about **one call per
    minute** by TPM. That's the real constraint that makes B binding, and it sets throughput
    (see Gate 1).
  * The daily cap for the whole project is shared with anything else using the same project.

## D12. Thought signatures: never edit assistant messages; drop or summarise only whole steps

*2026-11-09.* Source: <https://aistudio.google.com/docs/thought-signatures.md?generate-content=true>.
* "When using Gemini 4 and Gemini 3 models, you must pass back thought signatures during
  function calling, otherwise you will get a validation error (4xx)".
* "The **first** `functionCall` part in **each step** of the current turn **must** include its
  `thought_signature`", and omitting it fails with a 400.
* "Only current turn is required; we don't validate on previous turns".
* With parallel calls, only the first call carries a signature.
* Function-response `id`, `name` and count must match the calls they answer (Gemini 3.x best
  practices).

**Our agent's loop is a single user turn with many function-calling steps.** So every step is
part of the "current turn", and every assistant message that's sent must keep its signatures.

* **Decision.**
  * Assistant messages are replayed byte-for-byte from the provider's native content (D9).
  * Strategies may (i) mask or clip the *text* inside function responses, (ii) drop whole steps,
    meaning an assistant message together with all its function responses, and (iii) replace
    old whole steps with a summary.
  * They never split a call from its response, and never remove a signature from a step they
    keep.
  * The summary is a separate text part appended after the task statement in the first user
    message, so the task text stays verbatim (the pinned prefix).
* **Verification.** Whether the API accepts dropped middle steps and an added summary part is
  checked against each model by a manual integration test
  (`tests/integration/test_providers_live.py`). If a model rejects a form, that strategy can't be
  used with it, and this is reported at Gate 1.

## D13. countTokens isn't counted against RPM or TPM (measured); its RPD effect is unknown

*2026-11-09.* The docs (token counting and rate-limit pages) don't say whether `countTokens`
counts against limits. Measured (`docs/week3/evidence/api_probes_*.json`):
* **RPM:** 35 `countTokens` calls in 8.9 s on each Gemma model (limit 30 RPM), and 20 calls in
  5.7 s on Flash-Lite. No 429, and an immediate generation call succeeded each time.
* **TPM:** 86,673 tokens counted in about 1 s (above 5× Gemma's 16K TPM), with no 429, and the
  next generation call succeeded.

The RPD effect can only be seen on the AI Studio dashboard. It goes to the operator at Gate 1.

*Amended 2026-11-09, after the live tests.*
* **The SDK can't count the full request on the Developer API.** google-genai 2.33.0's
  `models.count_tokens` raises `ValueError: system_instruction parameter is only supported in
  Gemini Enterprise Agent Platform mode, not in Gemini Developer API mode`. The probes above
  counted plain contents only, so they never hit this. The first shadow-count implementation
  silently returned `None` on every call. It's fixed before any pilot data exists.
* **The REST method can.** `countTokens` accepts either `contents` or a full
  `generateContentRequest` (system instruction, tools, contents), but "never both"
  ([API reference](https://aistudio.google.com/api-reference/tokens/count-tokens.md), checked
  2026-11-09). The provider now posts a `generateContentRequest` directly.
  * **Accuracy:** on a multi-turn tool-calling prompt it counted **519 against 520** reported
    `prompt_tokens` on all three models, and 505 against 506 on another prompt
    (`tests/integration/test_providers_live.py`).
  * **Limits:** 30 full-request calls in 11.9 s on gemma-4-26b counted 1,166,700 tokens, about
    73 times the 16K TPM, with no 429
    (`docs/week3/evidence/count_tokens_full_request_gemma-4-26b-a4b-it.json`). That is
    consistent with the RPM/TPM result above.

* **Options.**
  * (a) The calibrated character estimator only.
  * (b) `countTokens` before every call.
  * (c) `countTokens` for Gemma, where the budget and TPM are tight, and the estimator for
    Flash-Lite, where RPD is scarce (500/day).
* **Proposal for Gate 1.** (c), with the estimator as the fallback whenever `countTokens` fails.
  This is decided at Gate 1 after the operator checks the dashboard.
* **Also measured.** A text sized as "12,000 tokens" by a words×2 rule was really 28,891 tokens.
  Naive estimates under-count badly, so the estimator starts conservative and is calibrated
  online (D15).

## D14. Clipping and masking are prompt-only; trajectories store full observations

* **Context.** Analysis must be able to reconstruct both what the tool returned and what the
  model actually saw. Week 2's environment truncates each observation to
  `max_observation_chars` (10,000) *before* storing it, and ends an episode when the total stored
  observation characters exceed `max_total_observation_chars` (1,000,000).
* **Decision.**
  * Week 3 experiments set the environment's per-observation cap high: 200,000 characters,
    which is a storage and safety cap, not a view. The total cap is raised to 20,000,000.
  * The model's view is built only by the ContextManager. It clips any single observation over
    2,000 tokens head+tail with an explicit marker, then applies the strategy's masking or
    summarisation.
  * For every LLM call, the trajectory records a compact view specification: per message,
    whether it was shown verbatim, clipped (and how much), masked, summarised or dropped, plus
    the summary texts. Because view building is deterministic, the exact prompt can be rebuilt
    from the full history and that record.
* **No silent conflict.**
  * If the environment cap ever truncates a stored observation, the step's
    `extra.repogym.truncated` is set and the episode is flagged `stored_observation_truncated`.
  * Hitting the total cap ends the episode with termination `harness_error`, never a context
    failure, so it's visible and can't be confused with the strategy's behaviour.
  * Loading an experiment refuses any `max_observation_chars` at or below the most the clip can
    ever keep. That's `clip_tokens x 2.5 chars/token / (0.3 lowest calibration factor x 1.05)`,
    about 15,900 characters for a 2,000-token clip, against a cap of 200,000. So the Week 2
    default (10,000) can't slip into a Week 3 experiment. Unit tests cover the check and every
    file in `configs/experiments/`.
* **Consequences.** Week 2's scripted agents keep their original budgets (Week 2 evidence is
  unchanged). The Week 3 settings live in the experiment config.

## D15. The token estimator starts conservative and is calibrated online, per episode

* **Context.** B is enforced before each call, but the API reports the true prompt size only
  afterwards. A naive estimate under-counts badly: a text estimated by words x 2 as 12,000 tokens
  was really 28,891 (D13 probe). Exact `countTokens` before every call is a Gate 1 decision
  (D13); an estimate is needed in any case, as the fallback and for Flash-Lite.
* **Options.**
  1. A fixed characters-per-token ratio.
  2. A local tokenizer (none is published for Gemini 3.5; Gemma's is, but it would cover only
     two of the three models).
  3. A conservative ratio, calibrated online against each call's reported `prompt_tokens`.
* **Decision.** (3):
  * Start at 2.5 characters per token, below the usual 3–4 for code and JSON, so the first
    calls over-count.
  * Add 8 tokens of per-message overhead and a 1.05 safety margin.
  * After each call, update a factor (reported / raw estimate) as an exponential moving average
    with alpha 0.3, clamped to [0.3, 3.0].
  * The estimator is created fresh for each episode. Episodes stay independent of the order they
    run in, so repeats are exchangeable and a resumed run behaves like an uninterrupted one.
  * Every call records estimated tokens, reported tokens, the relative error, and whether
    reported > B (a budget violation).
* **Consequences.**
  * The first one or two calls of an episode are the least accurate. At that point the prompt is
    far below B, so violations should cluster late, where the factor has converged.
  * `budget_violations` and the estimation-error mean and max are reported per episode and per
    strategy. If violations aren't rare (target: under 1% of calls) at Gate 1, the fallback is
    exact `countTokens` (D13).

## D16. The experiment runner: cells, resume, exclusion rules, concurrency

* **Context.** E1–E3 run for days under daily quotas, on one 7.7 GiB machine, and must survive
  restarts without duplicating or silently dropping episodes.
* **Decision.**
  * An experiment file defines a grid of cells (model x strategy x task x repeat), each with a
    stable key. Its provenance records the file's sha256 and the sha256 of the fully resolved
    config: tasks, context and budget settings, model settings, the limits file and margin, and
    the prompt texts and tool schemas.
  * A cell is done when its latest record exists and isn't an `api_failure`.
    * An interrupted episode leaves no record and reruns.
    * A `harness_error` is rerun **once**, and both records are kept and reported.
    * An `api_failure` from daily-quota exhaustion makes that model's worker sleep until the
      next midnight-Pacific reset plus 60 s, while the other models continue.
    * Any other `api_failure` pauses the worker for 60 s, then retries.
  * Each model has one worker, so at most 3 episodes, and 3 sandbox containers, run at once.
    That fits 7.7 GiB alongside Docker Desktop, and the free-tier limits are per model anyway,
    so more workers per model would only queue on the limiter.
  * Before anything runs, the guard enforces pilot hygiene (dev tasks only) and the test-split
    rules (frozen config, matching hashes in the pre-registration, a clean tree, and
    `--final-test-run`). Every refusal is appended to `data/runs/refusals.jsonl`.
  * The Week 2 bash-only `baseline` agent is refused on test-split tasks. Test-split LLM
    episodes come only from a frozen Week 3 experiment. The baseline itself is unchanged and
    still runs on dev and train.
* **Consequences.** Analysis uses each cell's latest record. A cell whose two records are both
  `harness_error` is excluded from success rates and listed separately. Pilot records
  (`experiment.pilot = true`) are excluded from all later analysis.

## D17. Episode limits: 75 steps; the wall-clock cap counts active time only

* **Context.**
  * The brief fixes a step cap of 75 and asks for a wall-clock cap.
  * Free-tier throttling is very uneven across models. Under Gemma's 16K input TPM (D11),
    late-episode calls near B = 12,000 admit about one per minute, while Flash-Lite (250K TPM)
    rarely waits at all.
  * A cap on elapsed time would end Gemma episodes because of free-tier throttling, not agent
    behaviour. That confounds RQ2 (models), and also RQ1, because strategies with larger
    prompts wait longer for TPM.
* **Options.**
  1. Cap elapsed time.
  2. Cap active time only.
  3. No time cap.
* **Decision.** (2), plus a separate safety net:
  * **Active time** is what `max_wall_seconds` caps. It's elapsed time minus throttling the
    agent reports through `env.record_wait`: model response latency, tool and test execution,
    and harness overhead.
    * The provider measures throttling exactly: the rate limiter's `acquire` returns how long it
      held the request, and retry backoff comes from the attempt log. `latency_seconds` is now
      API time only, with both removed.
    * Waits inside a call that ends in `BadResponse` are reported too.
  * **Waiting time** is recorded per call (`limiter_wait_seconds`, `backoff_seconds`,
    `waiting_seconds`, `call_wall_seconds`) and per episode
    (`accounting.waiting_seconds`, `api_seconds`, and
    `accounting.episode_seconds.{elapsed,active,waiting}`). It is reported only and never
    used for termination.
  * **`max_elapsed_seconds`** (6 h in Week 3 experiments; off for Week 2 runs) catches hung
    episodes. It can only fire when throttling or an outage stalls an episode, since active
    time has its own cap. So hitting it is classified as `api_failure`, never an agent outcome.
    * The runner retries the cell after that model's quota reset (D16).
    * After two elapsed-cap episodes, the cell is given up on and reported.
  * `max_steps = 75`. The pilot runs with an active-time cap of 3,600 s. The E1–E3 value is
    proposed at Gate 1 from the pilot's active-time distribution, set high enough to rarely
    bind.
  * Fake-clock unit tests show three things:
    * 21 calls each held 3,000 s by the limiter (about 17.6 h elapsed, 21 s active) still end
      `submitted` under a 600 s active cap;
    * slow API time without throttling still hits the active cap;
    * the 6 h elapsed cap ends a stalled episode, which maps to `api_failure`.
* **Consequences.**
  * `wallclock_cap` now means the agent itself was slow. It is reported per model and strategy.
  * Elapsed and waiting time are reported as throughput costs, for example to explain why
    Gemma runs take longer, but never count against success.
  * The environment's tool-command timeouts still use the remaining active time.

## D18. When a prompt leaves the summary out, it is recorded, counted and reported

* **Context.**
  * With `llm_summary` or `hybrid`, the context manager may, as a last resort, leave the
    stored summary out of one prompt so it fits B (found by the property test; see the
    strategies code).
  * The model then sees less than the strategy promises. If this happens often, the strategy
    being measured isn't really `llm_summary`.
* **Decision.**
  * Each occurrence emits a `summary_omitted` context event in the trajectory, an ATIF system
    step of type `pruning`/`truncate`.
  * The accounting counts it per episode (`summary_omitted`), with its rate per agent call
    (`summary_omitted_rate`), alongside `budget_violations`.
  * `repogym status` shows it, and the analysis reports its frequency per strategy and model
    in RESULTS.md next to budget violations.
  * **Threshold.** If it occurs in more than **2%** of the agent calls of any `llm_summary` or
    `hybrid` cell group, it is flagged at the next gate (Gate 1 for the pilot, Gate 2 for E1,
    Gate 3 for E3) as a threat to validity. It is not absorbed silently, and the proposed
    response is a larger B or a shorter summary, decided before any test-split run.
* **Consequences.** The reported success rate for a summarising strategy always comes with how
  often it actually ran as specified.

