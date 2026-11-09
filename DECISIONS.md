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

*Superseded 2026-11-09 by D20 (no `countTokens` in runs) and D21 (Gemma's re-billed thinking).*

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
    * *Amended 2026-11-09 (Gate 1 decision).* A cell gets **at most 3 `api_failure`
      attempts**. After the third it is recorded as `api_failure`, reported, and not retried.
      Daily-quota interruptions don't count toward the 3, because they come from our own quota
      scheduling, not the model; they're retried after the reset. The elapsed safety cap
      (D17) counts toward the 3.
  * *Amended 2026-11-09.* **Every attempt is kept.**
    * Each attempt has an index (`attempt`), and its trajectory and diff are named
      `<cell>__a<n>`. A name collision of any kind gets a suffix, so no file is ever
      overwritten. (The pilot's 31B rerun had overwritten attempt 1's trajectory.)
    * Each record's provenance lists the earlier attempts: index, episode id, termination,
      error type, steps, trajectory. `repogym status` reports attempts, cells retried and
      cells given up.
  * *Amended 2026-11-09.* **Heartbeat.** Each worker keeps `heartbeat/<model>.json` with its
    state (running, between episodes, sleeping until a time and why, done), the cell and
    attempt, the current step, and the time of the last model call. `repogym status` shows
    it, so a running episode is visible before its record exists.
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

* *Amended 2026-11-09, 15:37 UTC (operator decision): server errors and daily quota pause
  the call in place.*
  * **Evidence.** Gemma 26B's attempts failed with 5xx at 15 of 90 (17%) in the morning pilot.
    In the re-pilot's first attrs-1448 episode (about 13:30–13:41 UTC) the rate was **13 of
    22**: one call failed its 6 attempts within a few minutes, and the episode died at step 9.
  * **By UTC hour** (`scripts/reliability_by_hour.py`): 26B 14% at 11:00, 22% at 12:00, 47% at
    13:00, 46% at 14:00, 28% at 15:00; 31B 55–100% in every hour; Flash-Lite 0 of 225. Six
    consecutive 500s within minutes is a transient server problem, not an episode failure, and
    it used up the limited attempts per cell.
  * **5xx and transport errors** (not 429, not other 4xx) are retried **in place**: the
    episode, its sandbox and its state are kept.
    * Backoff is exponential with ±50% jitter, from **30 s**, capped at **600 s** between tries.
    * The call is declared failed (`ProviderUnavailable` → `api_failure`) only after **20
      consecutive failures or 60 minutes** since its first failure, whichever comes first.
    * With these values the 60-minute limit always comes first, after about 10–11 failures:
      the cumulative backoff passes 3,600 s by then. The 20-failure limit only binds if the
      backoff values are lowered.
    * The waiting is excluded from active time (D17), and the 6-hour elapsed cap still bounds
      it.
  * **Daily quota.** When the limiter finds the day's quota used up, or a 429 names a per-day
    quota, the call is **held in place until the reset plus 60 s** (at most once per call),
    then retried.
    * The episode keeps its sandbox and state. The container runs `sleep infinity`, so only a
      Docker restart could end it.
    * The hold is excluded from active time and also from the 6-hour elapsed cap, because it's
      a known, scheduled wait, not a hang. It is recorded as `quota_pause` in
      `episode_seconds`.
    * The runner's between-episode reset sleep remains only as a fallback.
  * **Pre-registered and configurable.** The values above are the experiment's `retry`
    section (`server_base_delay`, `server_max_delay`, `server_max_failures`,
    `server_max_seconds`, `pause_on_daily`, `max_daily_pauses`, `reset_margin`). They are part
    of the resolved config and its hash, the frozen keys, and each episode's provenance.
  * **Every attempt is logged** to `data/quota/attempts-<UTC day>.jsonl` with its time, model,
    status, kind and number within its call. It's the basis for error rates by hour, and for
    scheduling runs around bad hours.
  * **Applied from the 15:37 UTC restart of the re-pilot onward**, including tomlkit-536,
    which restarted from scratch as attempt 1. It came too late for attrs-1448: all three of
    its attempts ran under the old rule. Attempts 1 and 2 ended in `api_failure` at steps 9 and
    34; attempt 3 submitted at step 41, unsolved. Its outcome stands as recorded.

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

## D19. Agent fixes from the pilot diagnosis (approved at Gate 1)

* **Context.** In the first pilot, all 5 episodes reached the step cap without a source edit
  (Gate 1 report, section 5):
  * no reply contained any reasoning text;
  * 400-line `view_file` windows lost their middle to the 2,000-token clip;
  * repeats followed masking of the earlier result;
  * the first message described the 200,000-character storage cap as if it were the model's
    view.
* **Decision.**
  * **Reasoning.** The system prompt asks for one or two sentences with every tool call: what
    was learned, and what comes next. It explains that the agent's own messages outlive tool
    results.
    * A reply with a call but no text is **not rejected**: the call runs, and a
      `missing_reasoning` event is recorded with its streak.
    * After every second consecutive miss, the next tool result carries one short reminder (a
      `reasoning_reminder` event).
    * Both are counted per episode: `missing_reasoning`, `missing_reasoning_rate` per call,
      and `reasoning_reminders`.
  * **An accurate view description.** The system prompt is generated per strategy from the
    context configuration. It covers:
    * the clip size;
    * masking after M results (`obs_mask`, `hybrid`);
    * oldest steps removed (`sliding_window`, and the fallback);
    * the summary (`llm_summary`, `hybrid`).

    The environment's first message for this agent (`instruction_style="tool_agent"`) states
    active time and leaves out the storage cap. The Week 2 text is unchanged and tested.
  * **Step budget.** `[step k/N]` follows the last result of each reply, plus one nudge when 15
    calls remain (a `final_steps_nudge` event). These annotations are part of what the model
    sees. The stored observation is the environment's, unannotated (D14).
  * **`view_file`** shows at most 150 lines, about what the clip keeps.
  * All of these texts are in the resolved config (`prompts`) and frozen with it.
* **Consequences.**
  * Week 3 results come from this agent version. The first pilot is a diagnostic only.
  * The Week 2 bash agent and its prompt are untouched. `view_file` isn't one of its tools.

## D20. countTokens is never used in timed runs; our ledger counts our requests

*Decided 2026-11-09 (operator).*

* **Context.**
  * D13 proposed exact pre-call counts for Gemma.
  * The pilot showed the exact relation instead: billed prompt = counted tokens + Gemma's
    re-billed thinking (D21). So the estimator can be made accurate without calling
    `countTokens`.
  * A TPM charge for `countTokens` would roughly halve Gemma's throughput. RPD alone was never
    the concern.
* **Decision.**
  * No `countTokens` call in the agent loop or in any timed run. An experiment whose model
    settings ask for anything but `token_counting: estimate` is refused at load time.
    `countTokens` stays available for offline validation of the estimator.
  * **Our local quota ledger is the source of truth for our own request counts.** It counts
    every attempt that goes through the limiter, failed ones included.
* **Dashboard cross-check** (operator, 28-day peaks covering today):

  | Model | Dashboard RPD | Ledger | − 18 blocked locally (never sent) | + requests sent outside the limiter | Expected | Difference |
  |---|---|---|---|---|---|---|
  | gemini-3.5-flash-lite | 247 | 252 | 234 | +13 | **247** | 0 |
  | gemma-4-26b-a4b-it | 168 | 184 (13:02 UTC) | 166 | +10 | 176 | −8 |
  | gemma-4-31b-it | 159 | 172 (13:02 UTC) | 154 | +8 | 162 | −3 |

  * **Where the extra requests come from:**
    * the morning API probes: 8 generation calls per model, sent directly through the SDK;
    * for Flash-Lite, 5 debugging calls;
    * for 26B, 2 prototype calls.
  * **The ledger over-counts by 18 requests per model.** These were the first live-test run's
    attempts, which the unit-test network guard blocked before they left the machine.
  * **Flash-Lite matches exactly.** Its count stopped changing at 11:35 UTC, so this comparison
    doesn't depend on when the dashboard was read.
  * **The two Gemma differences fit a dashboard reading about 5–10 minutes before 13:02.**
    Both were still sending requests at 0.7–1.3 per minute. There's no sign of
    double-counting.
  * **`countTokens` doesn't count toward RPD.**
    * Flash-Lite made 25 `countTokens` calls today, and its count still matches exactly.
    * 26B made about 165 (probes, a 30-call burst, and the pilot's shadow counts), and its
      dashboard RPD of 168 is lower than its generation requests alone.
* **TPM cross-check.**
  * The dashboard shows peaks of 19.65K (26B) and 18.34K (31B) against 16K.
  * Rebuilt per calendar minute from the pilot trajectories (`scripts/pilot_quota_analysis.py`):
    * 26B went over in 3 of 72 minutes. Each time it was a **single** call, the largest
      reporting 16,943 tokens against an estimate under 12,000. The gap was re-billed
      thinking (D21).
    * The dashboard's higher peaks likely include the input of failed (5xx) attempts, which
      aren't in trajectories.
    * 31B's server errors are **not** quota-related: 0 of 17 came in a minute already over the
      limit; they came at the start of a minute with nothing else sent yet.
  * **The fix.**
    * The limiter already admits on the pre-call estimate and settles with the reported tokens.
    * Its admission estimate now includes re-billed thinking (D21), so no prompt can exceed
      B = 12,000. That is below 16K × 0.9 = 14,400.
    * The reservation for a failed attempt stays in the window, so a retry within the same
      minute waits instead of doubling the input.

* *Amended 2026-11-09, 16:00 UTC: a ledger bug, fixed.*
  * Each process kept an in-memory copy of `ledger.json` and rewrote the whole file on every
    request, so concurrent processes overwrote each other's counts.
  * It started mattering at 13:03 UTC, when the 31B probe began running beside the Gemma runner.
    Later the live tests and the thinking experiments ran beside them too.
  * **What it affects:**
    * The RPD cross-check above uses counts as of 13:02, while one runner process was writing,
      so it stands.
    * Ledger counts after 13:03 on 9 November are unreliable for gemma-4-26b, gemma-4-31b and
      gemini-3.1-flash-lite. None was near its daily limit (14,400, 14,400, and 500 with a
      handful used).
    * gemini-3.5-flash-lite had no requests after 13:02, so its count (252) is intact.
    * For that window the per-attempt log (D16; one appended line per attempt, safe across
      processes) is the source of truth.
  * **Fix:** every ledger read and update now holds an exclusive file lock and re-reads the
    file inside it. A unit test with 4 concurrent processes loses no counts; the old code lost
    most of them.
  * Processes started before the fix (the Gemma re-pilot runner and the 31B probe) keep the old
    behaviour until they end. Every run from 2026-11-10 08:05 UTC starts on the fixed code.

## D21. Gemma's earlier thinking is kept in the history and counted exactly

*Measured 2026-11-09; evidence in `docs/week3/evidence/thinking_history_gemma-4-26b-a4b-it.json`,
`scripts/probe_thinking_history.py`.*

* **Context.**
  * Gemma's replies contain a `text` part marked `thought: true` (its reasoning), next to the
    `function_call` part with its signature. We replay replies verbatim (D12).
  * In the pilot, the billed prompt exceeded `countTokens` by exactly the thinking tokens of
    the kept replies (+1), on all 75 calls.
* **Experiment.** The same third call of a real 2-step tool-calling episode on 26B, in six
  variants:

  | Variant | HTTP | Billed prompt tokens |
  |---|---|---|
  | A: full replay | 200 | 3,139 |
  | B: older signature replaced by the documented dummy | 200 (500 in one of three runs) | 3,139 |
  | C: all signatures replaced by the dummy | 200 | 3,139 |
  | D: older signature removed | 200 | 3,139 |
  | **E: thought parts dropped from both earlier replies, signatures kept** | 200 | **631** |
  | **F: thought part dropped from the older reply only** | 200 | **651** |

  * The earlier replies had thought 1,244 and 10 tokens.
  * Dropping the thought text saves exactly **2 × its thinking tokens** (2,508 and 2,488).
    The signatures don't affect billing.
  * Without its earlier thoughts, the model thought more on the next call (899 and 409 tokens
    against 10).
* **What the docs say.**
  * Gemini thinking docs: "Return the entire response with all parts back to the model". Only
    function-call signatures are strictly required.
  * **Gemma 4 model card**
    (https://ai.google.dev/gemma/docs/core/model_card_4, "Multi-Turn Conversations", checked
    2026-11-09): "Thoughts from previous model turns must not be added before the next user
    turn begins, **with the exception of tool call turns where thinking content should be
    preserved**."
  * Our episode is a single tool-calling turn (D12), so the model card says to keep the
    thoughts.
* **Decision.** Removal works technically, but the docs say to preserve thinking in tool-call
  turns, so we **keep the thought parts and count them exactly**:
  * each Gemma reply with a thought part carries `replay_tokens = 2 × thinking_tokens`;
  * the estimator adds that unscaled to every prompt containing the reply, and calibration
    sees only the text part;
  * the limiter admits on that estimate.
* **Consequences.**
  * Under B, Gemma's retained thinking competes with observations for room, so the drop
    fallback fires earlier for Gemma than for Flash-Lite. This is reported as a property of
    the setup, not hidden.
  * Lowering Gemma's thinking level (`minimal` is accepted) would free budget, but it changes a
    pinned vendor default (D10). It is not proposed.
* *Amended 2026-11-09 (Gate 1 decision): reported as a finding.*
  * The analysis measures the share of each prompt, and of B, taken by preserved thinking, per
    episode and per strategy. The source is `view.replay_tokens` against the reported
    `prompt_tokens` and B on every call: `accounting.preserved_thinking`, which also works
    after the fact from stored trajectories.
  * RESULTS.md reports it as a finding: **the cost of following vendor guidance under a hard
    budget**. The drop-the-thoughts measurement above (3,139 → 631 billed tokens for the same
    call) is cited as evidence of what is at stake.
  * The Gemma thinking page is even more explicit than the model card
    (https://ai.google.dev/gemma/docs/capabilities/thinking, checked 2026-11-09): "If a single
    model turn involves function or tool calls, thoughts must NOT be removed between the
    function calls."

## D22. Every model runs at its vendor-recommended thinking setting for coding tasks

*Decided 2026-11-09 (operator, Gate 1).*

* **Principle.** Each model uses the thinking setting its vendor recommends for coding tasks.
  Where the vendor documents none, it uses the API default. Settings are pinned in
  `configs/models.yaml`, recorded in every episode, and frozen before E3.
* **gemini-3.5-flash-lite: `high`.**
  * Gemini thinking docs (https://aistudio.google.com/docs/thinking.md?generate-content=true,
    checked 2026-11-09):
    * "Hard Tasks… such as solving complex math problems or coding tasks, we recommend setting
      a high thinking budget";
    * `minimal` is described as for "chat or high throughput applications" and "may think very
      minimally for complex coding tasks";
    * `thinkingLevel` is "recommended for Gemini 3 models";
    * Flash-Lite supports `minimal` (its default), `low`, `medium` and `high`.
  * `high` was accepted when probed (D10).
* **gemma-4-26b-a4b-it, gemma-4-31b-it: thinking on, at the API default (no level set).**
  * The Gemma 4 model card's best practices
    (https://ai.google.dev/gemma/docs/core/model_card_4, checked 2026-11-09) give the sampling
    settings and how to turn thinking mode on. They don't give a thinking level for coding or
    any other task type.
  * The Gemini thinking-level table doesn't list Gemma.
  * Thinking mode is the documented reasoning capability ("think step-by-step before
    answering"), and the Gemini API enables it by default for Gemma: measured, gemma-4-26b
    thinks unless told otherwise.
  * So the principle keeps the API default. That is also what D10 pinned. No change.
* **Condition: E3 is not the first run with Flash-Lite at `high`.**
  * The first pilot ran Flash-Lite at `minimal`. The re-pilot's Flash-Lite episodes stay at
    `minimal`, so the re-pilot isolates the agent fixes (D19).
  * After them comes a short check at `high`:
    * `configs/experiments/flash_thinking_high.yaml`;
    * the same 3 dev tasks, for a paired comparison;
    * tagged `pilot` and excluded from analysis;
    * set through `model_overrides`, which is hashed and recorded;
    * scheduled within Flash-Lite's daily quota by `scripts/run_when_quota.py`.
  * It reports reward, steps, latency per call, output and thinking tokens per call, any
    tool-calling or thought-signature problems, and requests used. That confirms `high` fits
    the daily quota and the active-time cap.
  * `configs/models.yaml` changes to `high` only after the check is reviewed, before the freeze.
* **Consequences.**
  * Flash-Lite's thinking costs output tokens and latency, but no RPD or TPM; TPM counts input.
  * If Flash-Lite turns out to replay thinking the way Gemma does, the D21 accounting
    covers it. The check will show it either way.

