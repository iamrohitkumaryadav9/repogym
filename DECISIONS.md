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
  * A unit test checks that the environment cap is above the prompt clip limit. The clip is
    2,000 tokens, about 8,000 characters at the conservative rate; the cap is 200,000 characters.
* **Consequences.** Week 2's scripted agents keep their original budgets (Week 2 evidence is
  unchanged). The Week 3 settings live in the experiment config.
