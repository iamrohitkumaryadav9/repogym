# Week 3: context management under a hard token budget

Week 3 is a small, pre-registered study, not just a leaderboard. The hypotheses, experiments,
selection rule and statistics are fixed in [PREREGISTRATION.md](PREREGISTRATION.md) before any
RQ1 or RQ2 run. Design decisions are in [DECISIONS.md](../../DECISIONS.md), from D8 onward.

## Questions

**RQ1 (primary): context management under a hard budget.** Every prompt sent to the model must
fit within a fixed token budget B. Which context-management strategy gives the best solve rate,
and at what token cost?

* **Prior work.** Lindenbauer et al., *The Complexity Trap: Simple Observation Masking Is as
  Efficient as LLM Summarization for Agent Context Management* (JetBrains Research, DL4C
  workshop at NeurIPS 2025, [arXiv:2508.21433](https://arxiv.org/abs/2508.21433)). With
  SWE-agent on SWE-bench Verified, they found that observation masking roughly halves cost
  compared with the raw agent, while matching LLM summarisation. A hybrid of the two reduced
  cost further.
* **What we add.** We test the same question:
  * (a) under a **hard per-prompt budget**, rather than an unbounded context;
  * (b) on a **new, contamination-controlled benchmark** (RepoGym v0.1, with time-based splits);
  * (c) in the **small-model regime**.

**RQ2 (secondary): model comparison.** With the context strategy frozen from RQ1, how do three
free-tier models compare on the held-out test split?

**RQ3 (exploratory): contamination signal.** Within each model, do tasks merged before the
model's documented knowledge cutoff pass more often than tasks merged after it? This is
exploratory and has low statistical power; see the pre-registration.

## Strategies compared (RQ1)

`sliding_window` (the budgeted baseline), `obs_mask`, `llm_summary` and `hybrid`. All four share
a pinned prefix (system prompt, tool schemas and task statement, kept verbatim), always keep the
latest step in full, never split a call from its response, and apply the same per-observation
clip. That clip isn't a variable in the comparison. See D12 and D14.

## Models

All three use the Gemini API free tier. Each has its own rate limits, and all three run in
parallel.

| Model ID (from `models.list`, 2026-11-09) | Kind | Free-tier limits (operator's AI Studio dashboard) | Knowledge cutoff |
|---|---|---|---|
| `gemini-3.5-flash-lite` | proprietary | 15 RPM · 250K TPM · 500 RPD | March 2026, with some domains limited to January 2025 ([model card](https://deepmind.google/models/model-cards/gemini-3-5-flash-lite/), published July 2026) |
| `gemma-4-31b-it` | open weights, dense, 30.7B | 30 RPM · 16K TPM · 14.4K RPD | training data to January 2025 ([Gemma 4 model card](https://ai.google.dev/gemma/docs/core/model_card_4)) |
| `gemma-4-26b-a4b-it` | open weights, MoE, 25.2B total / 3.8B active; the RQ1 workhorse | 30 RPM · 16K TPM · 14.4K RPD | training data to January 2025 (same card) |

TPM counts **input** tokens, and quotas reset at midnight Pacific time (D11). Gemma's 16K TPM is
what makes a hard per-prompt budget a real constraint here rather than an artificial one: a
prompt much over 12K tokens can't be sent more than once a minute at all.

## Not comparable with Week 2

The Week 2 baseline smoke test used a **bash-only, text-parsed agent** (one ```bash block per
turn, no native function calling, no context management, unbounded linear history). Its numbers
**are not comparable** with Week 3. Week 3 uses a different agent: native function calling over
all six tools, through a ContextManager, under a budget B and a different step cap.

The Week 2 baseline agent (`repogym run --agent baseline`) is kept runnable and unchanged.

## Reproducing

The commands (`repogym run --experiment …`, `repogym status`, `repogym analyze`, `make results`)
are documented here as they land. Pilot runs are tagged `pilot` in provenance and excluded from
all later analysis.
