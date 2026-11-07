# RepoGym

An RL environment and evaluation harness for coding agents, built from real GitHub data.

## Week 1: Task mining and validation

The Week 1 pipeline turns merged GitHub pull requests into verified tasks. Each task is an issue, a
repository at the commit before the fix, and tests that prove whether a patch fixes it.

```
repogym mine      ->  data/tasks_candidates.jsonl   (GitHub REST API, cached)
repogym build     ->  repogym/<repo>:base images + bare clones under data/repos/
repogym validate  ->  data/tasks_verified.jsonl and data/rejections.jsonl
repogym stats     ->  counts, rejection reasons, medians, Docker disk usage
repogym clean     ->  remove repogym's stopped containers and unused images
```

Repositories: pallets/click, pylint-dev/astroid, PyCQA/isort, python-poetry/tomlkit and
python-attrs/attrs. All are pure Python with fast pytest suites. marshmallow-code/marshmallow is
configured as a reserve but not mined.

### Mining

`repogym mine` lists closed pull requests created in the last three years and keeps merged ones that:

1. **Link to an issue.** It looks for GitHub closing keywords ("fixes #N", "closes owner/repo#N", or
   an issue URL) in the title and body, ignoring HTML comments and code, where PR templates show
   placeholder examples. If there are none, it reads the PR timeline for issues linked by hand in the
   Development sidebar (`connected` events, minus later `disconnected` ones). The linked issue must
   be closed, in the same repo, and an issue rather than a pull request.
2. **Change both a source file and a test file.** Paths in a `test`/`tests` directory are tests; paths
   under the repo's package directory are source; anything else (docs, changelogs, config, lock
   files) goes in neither patch.
3. **Change fewer than 300 lines** of source and test code combined.

PRs with binary changes are also dropped, because a binary change can't be applied from a text diff.
The PR's diff is split into `gold_patch` (source files) and `test_patch` (test files), and
`base_commit` is the PR's base SHA. Every API response is cached under `data/cache/`, so reruns
make almost no requests. The client sleeps until the rate limit resets when it gets low, and backs
off on server errors. Per-filter drop counts are in `data/mining_report.jsonl`.

### Environments: one image per repo

Disk is limited, so there is **one image per repository, not per commit**. `repogym/<repo>:base` is
`python:3.11-slim` plus git and the repo's test dependencies, pinned in
`src/repogym/env/requirements/<repo>.txt`. It contains none of the repo's own code. Each image is
about 400–600 MB, and all five come to about 2.4 GB.

The code comes from a single bare clone per repo under `data/repos/`, mounted read-only into every
container. Each run starts a fresh container that:

1. creates `/work`, points it at the bare clone's objects (git alternates, so nothing is copied),
   and checks out `base_commit`;
2. checks out submodules from local mirrors (tomlkit's `toml-test` suite), so no network is needed;
3. runs `pip install -e .` offline, using the build backends baked into the image;
4. applies the patch under test, then `test_patch`, with `git apply`;
5. runs only the test files `test_patch` touches, and reports each test's result from pytest's JUnit
   XML.

Containers run with `--network none`, a 2 GB memory limit, 2 CPUs and a 300-second timeout, and are
always removed afterwards. If setup fails (checkout, install, or a patch that doesn't apply), the
task is rejected with that stage as the reason, and validation carries on. Commit-specific images are
supported in the design but none have been needed. Every image build is logged to
`data/image_builds.jsonl`.

Click's old tests break under pytest 9, which deprecates a parametrize pattern they use, and click
treats warnings as errors. Its base image therefore pins `pytest<9`, so no click task needs its own
image.

### The three validity checks

For each candidate, the validator first runs a **baseline**: `base_commit` with no patches, running
the touched test files that already exist. Any test that fails there, or whose module fails to import
there, is an environment problem rather than part of the task. It can never enter either list, and
it is logged to `data/baseline_failures.jsonl`.

* **FAIL_TO_PASS**: fails, errors, or can't be imported with `test_patch` alone; passes with the fix.
* **PASS_TO_PASS**: passes with `test_patch` alone, and still passes with the fix.

1. **Unsolved-state check**: `base_commit + test_patch` must leave FAIL_TO_PASS tests failing. If
   nothing fails, the tests don't exercise the bug, and the task is rejected.
2. **Oracle check**: `base_commit + gold_patch + test_patch` must pass every FAIL_TO_PASS and
   PASS_TO_PASS test. This runs **three times**, and any test whose result differs between runs gets
   the task rejected as flaky.
3. **No-op check**: `base_commit + empty patch + test_patch`, as a separate run, must not count as
   solved. If some FAIL_TO_PASS tests pass here, the task is rejected as flaky.

Only tasks that pass every check are written to `data/tasks_verified.jsonl`. Every rejection is
written to `data/rejections.jsonl` with its reason and the first error message.

Validation is **resumable**. Each result is flushed to disk (fsync) as soon as it finishes, and a
rerun skips any task already in either file. If Docker itself fails, for example because the laptop
slept, the run stops without recording those tasks, so they are retried next time.
`repogym validate --retry <reason>` re-validates tasks rejected for a given reason, such as
`unsolved_timeout`.

### WSL2 setup

* Keep the project in the Linux filesystem (`~/repogym`), not `/mnt/c`. Bind mounts and git are much
  faster there, and line endings stay LF.
* Turn on Docker Desktop's WSL integration for the distro. If `docker` reports permission denied
  right after doing so, open a new WSL session, because group membership only takes effect at login.
* `core.autocrlf=input` and `.gitattributes` (`eol=lf`) keep patches applying cleanly.
* With about 7.7 GB of RAM available to WSL, the default is 2 workers × 2 GB per container.
  `--workers` changes it.

### Reproduce

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # once
uv sync
echo 'GITHUB_TOKEN=...' > .env                        # a token with public read-only access
uv run repogym mine                                   # ~35 min uncached, ~3,100 API requests
uv run repogym build                                  # bare clones + 5 base images, ~4 min
uv run repogym validate --workers 2                   # ~75 min for 271 candidates
uv run repogym stats
uv run pytest                                         # unit tests, no network
```

### Week 1 results

| Repo | Merged PRs (3 yrs) | Candidates | Verified |
|---|---|---|---|
| pallets/click | 349 | 75 | 69 |
| pylint-dev/astroid | 765 | 73 | 66 |
| python-poetry/tomlkit | 205 | 49 | 44 |
| PyCQA/isort | 275 | 49 | 43 |
| python-attrs/attrs | 200 | 25 | 21 |
| **Total** | **1,794** | **271** | **243** |

Rejections: 17 where nothing fails without the fix, 4 where the fix doesn't make the tests pass, 3
with no runnable test file, and 3 timeouts plus 1 out-of-memory, all in the unsolved run. The median
verified task changes 45 lines, and its touched tests run in 1.7 seconds.

Not every verified task is a bug fix. The filters accept any merged PR that links an issue, and 9
verified tasks come from issues labelled as features or enhancements.
