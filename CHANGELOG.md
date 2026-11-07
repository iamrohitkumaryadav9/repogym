# Changelog

## v0.1 (unreleased draft)

First frozen dataset: 247 verified tasks from 5 Python repositories, with time-based splits.

### Dataset
- 247 verified tasks: pallets/click 71, pylint-dev/astroid 67, python-poetry/tomlkit 45,
  PyCQA/isort 43, python-attrs/attrs 21. They come from 1,794 merged PRs over 3 years and 271
  candidates, so 91.1% of candidates survive validation.
- 24 rejections: 19 where nothing fails without the fix, 3 with no runnable test file, and 2 where
  the fix doesn't make the tests pass.
- Splits by PR merge date, with whole UTC days never split:
  - train: 172 tasks merged before 2026-07-10
  - dev: 25 tasks merged 2026-07-10 to 2026-08-17
  - test: 50 tasks merged on or after 2026-08-18
- `task_type`: bug 216, feature 22, other 5, unknown 4.
  - Sources: human 30, github 55, classifier 158, unknown 4.
- `spec_clear`: 239 clear, 8 unclear.
  - Sources: human 25, heuristic 222.

### Validation
- One base image per repo with pinned test dependencies, `--network none`, 2 GiB and 2 CPUs.
- Validation runs a baseline, then the unsolved, oracle (×3) and no-op checks. Tests that fail at
  the baseline are excluded from both lists.
- A 30 s per-test timeout counts as a failure. A 300 s container backstop applies. Every listed
  test must stay under 10 s in all oracle runs.
- OOM rule: an OOM kill in the unsolved or no-op run counts as a failure only if every oracle run
  peaks below 50% of the memory limit. `astroid-2596` is verified under this rule.
- Every task has provenance: image digest, repogym commit, pytest version and a UTC timestamp.
- All 271 candidates were re-validated on the final images. The only change was `astroid-2596`,
  which went from rejected to verified.

### Labelling and calibration
- Gemini task-type classifier (`gemini-3.5-flash-lite`, prompt `task-type-v1`, temperature 0):
  - Strict JSON validation, plus `finishReason` checks, before anything is cached.
  - Retries with exponential backoff and jitter, with throttling.
- Three-run consensus for the 174 tasks without a GitHub type label. A label is applied only if
  all 3 runs agree with confidence ≥ 0.9.
  - Applied: 170 of 174 (bug 150, feature 15, other 5). Dropped for disagreement: 3. Low confidence: 1.
    Failed run: 0.
  - Run-to-run agreement: all 3 runs agree on 171/174 tasks (98.3%), pairwise 516/522 (98.9%); the consensus matches the human label on all 12 tasks where both exist.
- Calibration against 30 blind human labels: accuracy 0.90, κ 0.75. Against GitHub labels:
  accuracy 0.97, κ 0.88 (n = 73).
- Spec check against 25 blind human labels: precision 0.67, recall 0.75, F1 0.71.
  - 3 false positives were adjudicated, and the human labels were upheld in all 3.
- `gemini-3.8-flash` was dropped as the official model because of free-tier 429 limits.

### Tooling
- `repogym release <version> [--dry-run]` writes `tasks.jsonl` and `manifest.json`, then
  validates the release against its manifest.
  - The manifest records the sha256, image digests, split cutoffs, label sources and classifier.
- GitHub Actions CI runs lint and unit tests on Python 3.11 via uv, with no network and no Docker.
- `make reproduce` runs every stage end to end.
