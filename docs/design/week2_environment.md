# Week 2 design: agent environment, tools, grading and trajectories

Status: **draft for review**. Nothing here is implemented yet.

This document describes how a verified task from `data/release/v0.1/tasks.jsonl` becomes a
sandboxed episode. An agent works on the task through tools, and the result is a graded,
standard-format trajectory. Each decision is tied to a failure mode that has been seen in public
benchmarks, mainly SWE-bench and SWE-Bench Pro. The scripted-agent plan in §8 is how we show the
environment is correct before any LLM agent runs.

## 0. Failure modes this design must prevent

| # | Failure mode | Where it was seen | Defence (section) |
|---|---|---|---|
| F1 | Agents run `git log --all` and read *future* commits that contain the fix. These come from branches tracking a remote, which survive `git reset --hard`. | SWE-bench [#465](https://github.com/SWE-bench/SWE-bench/issues/465). The fix in [PR #471](https://github.com/SWE-bench/SWE-bench/pull/471) cut history by timestamp. | §3.1: history is rebuilt from ancestors only, then asserted |
| F2 | The reflog and tags expose future commit messages or hashes. | SWE-bench #465 | §3.1 |
| F3 | Deleting refs doesn't delete objects: the fix is still in `.git/objects` and recoverable by hash. | [SWE-Bench Pro Verified](https://arxiv.org/html/2609.08149) | §3.1: the clone never contains those objects, which is asserted |
| F4 | Feature branches (`origin/dev`) carry the fix, and images leak 97 to 16,000 post-base commits. | SWE-Bench Pro [#93](https://github.com/scaleapi/SWE-bench_Pro-os/issues/93), [oceanbase #2363](https://github.com/oceanbase/powercontext/issues/2363) | §3.1 |
| F5 | Network access lets an agent fetch the upstream fix or a newer release (`pip install -U`, `git fetch`, `curl` to GitHub). | Same reports | §3.3 |
| F6 | Agents edit the tests until they pass, for example by replacing assertions. | General reward hacking; SWE-bench grades by overwriting test files | §5 step 2 |
| F7 | Agents change test *infrastructure* outside the test files, such as a `conftest.py` hook, pytest config, `sitecustomize.py`, or a module that shadows `pytest`. Overwriting the test files alone doesn't stop this. | Known pytest reward hacks | §5 step 2 (an extension of the brief; see §10) |
| F8 | **A released copy of the target library is already in the image.** Found in our own images: `repogym/tomlkit:base` ships `tomlkit 0.15.2` in site-packages, which is newer than most tomlkit base commits. | RepoGym image scan, 2026-11-07 | §3.2 |
| F9 | Grading trusts the agent's container. The agent can leave background processes, patch site-packages, or plant git hooks, filters or `diff.external` config that run as root during diff extraction. | General sandbox hygiene | §5 steps 0–1 |

## 1. Environment lifecycle

```
reset(task_id)
  1. create a fresh container from the task's image, which is still stopped
     (--network none, 2 GiB, 2 CPUs, --pids-limit 512, --cap-drop ALL,
      --security-opt no-new-privileges, no mounts, random name and hostname)
  2. docker cp in a git bundle that holds ONLY base_commit and its ancestors
     (plus pinned submodule commits), then start the container
  3. as root: clone the bundle into /work, strip it (§3.1), pip install -e . (offline,
     as in validation), create the unprivileged user `agent`, chown /work to it,
     and write the setup marker /tmp/.repogym-setup
  4. run every leakage assertion (§3). Any failure means the setup fails loudly
     (EnvSetupError); no episode starts
  5. return an Observation: the issue title and body, the repo location (/work),
     the tool list, and the budgets
step(action)  ->  (Observation, done, info)
  - one tool call; it runs as `agent` via docker exec, with host-side timeout and truncation
  - budgets are checked before and after every step (§6)
  - submit() or an exhausted budget gives done=True
state()       ->  EpisodeState (episode_id, task_id, step_count, budgets used, done, flags so far)
close()       ->  extract the agent's work (§5 step 1), then remove the container
grade()       ->  runs in a SEPARATE fresh container (§5); returns a Grade
```

* **No mounts in the agent's container.** Validation mounts the bare clones read-only, but the
  agent's container never gets them, and never gets `data/` or any host path. The bundle is copied
  in and deleted after cloning.
* **The agent runs as a non-root user** with all capabilities dropped. It can't write to
  site-packages, change file ctimes, or undo the setup. Root is used only by the harness itself,
  for setup and for checks, through `docker exec -u 0`.
* **Hidden data stays on the host.** The task's `gold_patch`, `test_patch`, FAIL_TO_PASS and
  PASS_TO_PASS lists and metadata are never written into the agent's container, its environment
  variables, its name, labels or hostname. The `Environment` object holds them host-side and uses
  them only in `grade()`.
* **OpenEnv compatibility.** The API is synchronous `reset`/`step`/`state`, with Pydantic
  `Action`, `Observation` and `EpisodeState` models. These map one-to-one onto OpenEnv's contract:
  `reset() -> observation`, `step(action) -> observation` with `reward` and `done`, and
  `state()` with `episode_id` and `step_count`. OpenEnv environments are normally FastAPI servers
  in a container. If `openenv-core` installs cleanly on Python 3.11, I'll add an in-process
  adapter. Otherwise I'll document the mapping and leave the HTTP wrapper for later.

## 2. Tool interface

These are the six tools from the brief. One `Tools` implementation is used both by
`Environment.step()` (in-process, for scripted agents and tests) and by the MCP server.

| Tool | Behaviour | Errors (informative) |
|---|---|---|
| `bash(command)` | `bash -lc` in `/work` as `agent`, with the per-command timeout. Returns exit code, stdout and stderr, each truncated (§6) | Timeout: "killed after N s"; non-zero exit is reported, not raised |
| `view_file(path, start_line, end_line)` | Numbered lines, at most 400 lines per call, paths relative to `/work` | Missing file, a directory, binary content, a range out of bounds (with the real line count), a range too large |
| `str_replace(path, old, new)` | The match must be exact and unique | 0 matches; N > 1 matches with their line numbers; `old == new`; file missing |
| `create_file(path, content)` | Creates or overwrites a file, creating parent directories | Path outside `/work`, path is a directory |
| `run_tests(target)` | `python -m pytest` on a file or node id **that exists at base_commit**, with the same per-test timeout as validation | Target not tracked at base_commit, which explains that only existing tests can be run this way and points to `bash` for new ones; pytest output truncated |
| `submit()` | Ends the episode | |

* File tools take the content through stdin, never through shell quoting. They resolve paths
  inside the container and refuse anything outside `/work`. `bash` can still reach the rest of
  the container; §5 explains how that's detected.
* **Where the tools run.** I propose that the tools act inside the sandbox via `docker exec`, with
  **the MCP server on the host**, using the official `mcp` Python SDK over stdio. The alternative,
  an MCP server inside the container, would need the `mcp` package and its dependencies installed
  in the *validated* images. That would change their digests and test environments, which would
  invalidate v0.1. Keeping the server on the host also means budgets and truncation are enforced
  where the agent can't tamper with them. *(Decision D1.)*
* Every tool call and its result go into the trajectory with timestamps.

## 3. Anti-leakage measures, asserted before the first action

### 3.1 Git history (F1–F4)

The history isn't stripped from a full clone. It's **built so that future objects never enter
the container in the first place**:

1. On the host, create a bundle from the bare clone that contains only a temporary ref set to
   `base_commit`, using `git bundle create task.bundle repogym-base`. A bundle holds exactly the
   objects reachable from its refs, which here means `base_commit` and its ancestors.
2. In the container: `git clone task.bundle /work`, check out `base_commit` on a branch named
   `repogym`, then:
   * remove every remote;
   * delete every other branch and every tag;
   * `git reflog expire --expire=now --all` and `git gc --prune=now --aggressive`;
   * remove `ORIG_HEAD`, `FETCH_HEAD` and `packed-refs` leftovers, and the bundle file.

   Submodule repositories (tomlkit's `toml-test`) get the same treatment at their pinned commit.
3. **Assertions.** Any failure stops the setup:
   * `git remote` is empty, `git tag` is empty, and the only ref is `refs/heads/repogym`, which
     equals `base_commit`.
   * `git rev-list --all --not <base_commit>` is empty. This is the topological statement "no
     commit that isn't an ancestor is reachable". It is stricter than SWE-bench's timestamp
     cutoff, because it also removes fixes authored *before* the base commit on other branches.
   * **Object-level check:** every commit object in the store, listed with
     `git cat-file --batch-all-objects --batch-check`, is an ancestor of `base_commit` or the base
     itself. This catches F3 even with no refs at all. `git fsck --unreachable` finds nothing.
   * The reflog is empty for every ref.
   * **Gold-patch probe:**
     * `git patch-id` of the gold patch doesn't match the patch-id of any reachable commit.
     * The gold patch's *distinctive* added lines must not appear as added lines in
       `git log --all -p`. "Distinctive" means at least 20 characters after stripping whitespace,
       and not present anywhere in the tree at `base_commit`.
   * Every assertion is recorded with its counts in the episode record.
4. **Known edge case.** A fix that *restores* code deleted earlier, such as a revert of a
   regression, has added lines that legitimately appear in the past history. The line probe would
   then fail setup. I'll run the probe over all 247 tasks during Step 1 and report any task where
   it fires. I won't relax the probe silently. *(Decision D6.)*

### 3.2 Hidden data and installed copies (F8)

* None of the following is in the container: the gold patch, test_patch, test lists, task id, PR
  or issue numbers, any data file, or a bare clone. The issue text is given to the agent as an
  observation and isn't written to disk.
* **Installed-copy assertion:**
  * After `pip install -e .`, the target package's import resolves to `/work`.
  * `pip` lists exactly one distribution of it, the editable one.
  * A filesystem scan finds no other copy of its top-level package anywhere outside `/work`: no
    site-packages directory, wheel, sdist or pip cache.
  * For tomlkit this means checking that pip really removed the image's `tomlkit 0.15.2`. If it
    didn't, setup fails. Copies of *other* repos' packages, such as click in the isort image, are
    allowed, because they aren't the target.
* A probe also searches the container for files that look like task data: `*.patch`, `*.diff`,
  `tasks*.jsonl`, and the gold patch's distinctive lines under `/`. It should find nothing.

### 3.3 Network (F5)

* The container is created with `--network none`, and `docker inspect` must show `NetworkMode`
  as `none`.
* Inside, as the agent user, the setup checks expect all of these to fail:
  * DNS resolution of `github.com` and `pypi.org`;
  * a TCP connect to `1.1.1.1:443`;
  * an HTTP request to `https://pypi.org/simple/`;
  * `pip download` of the target package.
* `/sys/class/net` must contain only `lo`.

### 3.4 Filesystem

* `docker inspect .Mounts` must be empty, and `/proc/mounts` must show no bind mount of a host
  path.
* At the end of the episode, `find / -xdev -cnewer /tmp/.repogym-setup` (as root, excluding
  `/proc`, `/sys` and `/work`) lists every path the agent changed outside the repo. It uses
  *ctime*, which a non-root agent can't forge.

## 4. Budgets

All budgets are configurable, defaults in `config.py`, and the values are recorded per episode:

| Budget | Default | On exhaustion |
|---|---|---|
| `max_steps` | 100 tool calls | Episode ends, flag `budget_exhausted: steps` |
| `max_wall_seconds` | 1800 | The running command is killed and the episode ends |
| `max_total_observation_chars` | 1,000,000 | Episode ends |
| `command_timeout_seconds` | 120 (`run_tests`: same as validation, 30 s per test and 300 s overall) | Command killed; the observation says so |
| `max_observation_chars` | 10,000 | Head and tail kept, with `[... N characters truncated ...]` in the middle |
| LLM tokens and cost | Recorded per step, with optional `max_cost_usd` | Episode ends |

Cost is computed from a configured price table. A model with no listed price records
`cost_usd: null`; the cost is never guessed.

## 5. Grading (`grade.py`), always outside the agent's view

0. **Grading never runs in the agent's container (F9).** It runs in a fresh container from the
   same image, set up exactly like validation: base_commit, `pip install -e .`, and the same
   runner. The OOM and timeout rules and the JUnit parsing are therefore identical.
1. **Extract the diff without trusting the agent's git (F9).**
   * `docker cp` the working tree of `/work` out to the host, excluding `.git`.
   * Copy it over a pristine host checkout of `base_commit`, then
     `git add -A && git diff --cached --binary base_commit`.
   * The ignore rules come from `base_commit`'s `.gitignore`, so the agent can't hide files
     through `.gitignore`.
   * Git hooks, filters, textconv and `diff.external` from the agent's repo are never executed,
     because its `.git` never leaves the container.
   * Commits the agent made are covered automatically, since only the final tree matters. If its
     `HEAD` moved off `base_commit`, that's recorded as flag `agent_committed`.
2. **Sanitise before applying test_patch (F6, F7).** In the grading container, after applying the
   agent's diff:
   * `git checkout <base_commit> -- <paths>` for every test_patch path that exists at base, and
     `rm` for test_patch paths the agent created. This is the brief's rule, using base_commit
     rather than HEAD.
   * **Proposed extension *(Decision D3)*:** the same revert also covers changes to:
     * every test file (the mining classifier's test paths);
     * every `conftest.py`;
     * pytest configuration (`pytest.ini`, `tox.ini`, `setup.cfg` and the
       `[tool.pytest.ini_options]` table in `pyproject.toml`);
     * `sitecustomize.py`, `usercustomize.py` and `*.pth` files;
     * any new top-level module or package whose name shadows a stdlib or installed module, such
       as `pytest.py`.

     Each revert is flagged with its paths. A legitimate fix never needs these files, because the
     hidden tests come with test_patch.
3. Apply test_patch with `git apply`. If it doesn't apply, the result is `reward 0`, flag
   `test_patch_conflict`.
4. Run FAIL_TO_PASS and PASS_TO_PASS with the validation per-test timeout (30 s) and OOM rules.
   * A skipped or xfailed FAIL_TO_PASS test is **not** a pass.
   * A test that never ran, for example because of an OOM kill, counts as failed.
5. **Reward:** 1 only if every FAIL_TO_PASS test passes and every PASS_TO_PASS test still passes.
   Otherwise 0.
   * Partial metrics are recorded: fraction of FAIL_TO_PASS passed, and the number and names of
     PASS_TO_PASS regressions.
   * Infrastructure errors (Docker failing, an image missing) give `status: error` with **no
     reward**. They're excluded and retried, never counted as 0.
6. **Flags.** Each flag carries evidence (step id, command, paths):

   | Flag | How it's detected |
   |---|---|
   | `test_files_edited` | The diff touches test or test-infrastructure paths (list); they're reverted |
   | `agent_committed` | `HEAD` or refs in the agent's repo changed |
   | `git_history_commands` | `bash` commands matching git history or ref inspection: `log --all`, `reflog`, `fsck`, `show <hash>`, `branch`, `tag`, `remote`, `fetch`, `cat-file`, `rev-list` |
   | `network_attempts` | Commands matching `curl`, `wget`, `pip install` or `download`, `git fetch`, `pull` or `clone`, `nc`, or Python `urllib`, `requests`, `socket` or `http`; plus outputs showing name-resolution or unreachable-network errors |
   | `task_data_search` | Commands searching outside `/work` for patches, diffs, jsonl or task names (`find /`, `grep -r /`, `locate`) |
   | `files_outside_repo` | The ctime scan in §3.4 (paths) |
   | `budget_exhausted` | Which budget |
   | `gold_content_in_observations` | Any distinctive gold line appearing in any observation the agent received. This is a leak detector and must be 0 for every non-oracle agent |

   The command-based flags are **heuristics for analysis**. The *enforcement* is what's described
   in §3. An obfuscated attempt might not be flagged, but it still can't succeed.

**Out of scope.** A source change that special-cases the test run, such as
`if "pytest" in sys.modules`, can't be detected in general. As a heuristic, I'll flag source diffs
that reference `pytest`, `sys.modules` or `PYTEST_CURRENT_TEST`.

## 6. Truncation

Every observation (stdout, stderr and tool messages) is truncated to the budget. The first and
last halves are kept, and the marker in the middle states how many characters were removed. The
full output is never kept anywhere the agent can read it. Truncation is applied on the host, so
the agent can't avoid it.

## 7. Trajectory format

* **ATIF, version v1.8.** This is the version the Harbor RFC
  ([rfcs/0001-trajectory-format.md](https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md))
  marks as current. `schema_version` is `"ATIF-v1.8"`.
  * **Structure:** step 1 is the `system` step with the instructions and tool definitions. Step 2
    is the `user` step with the issue. Each later `agent` step has its `tool_calls`, with an
    `observation.results[].source_call_id` that links to the call's `tool_call_id`.
  * **Scripted agents** use `llm_call_count: 0` and omit `metrics`, as the RFC requires for
    deterministic dispatch.
  * **LLM steps** carry `metrics` (`prompt_tokens`, `completion_tokens`, `cost_usd`).
  * The reward, partial metrics and flags go in `final_metrics.extra.repogym` and in the root
    `extra.repogym` (task_id, release version, episode_id).
* **Validation.** Harbor itself can't be a dependency. Its PyPI package requires Python ≥ 3.12
  and brings in 106 dependencies, including LiteLLM and Supabase, and its newest release is less
  than two weeks old. So:
  1. RepoGym will include a validator written from the RFC, covering every required field, type,
     allowed value and cross-rule (step ids from 1, source-specific fields, call-id links,
     ContentPart rules). It runs in CI.
  2. A local cross-check (not in CI, since it needs the network) will run Harbor's own validator,
     `python -m harbor.utils.trajectory_validator`, in an isolated
     `uvx --python 3.12 --from harbor==0.24.0` environment. That release is from 2026-10-05.
     If 0.24.0 only knows ATIF ≤ v1.7, I'll target the newest version it accepts and record that
     choice. *(Decision D5.)*
* **RepoGym episode record (JSONL).** One line per episode, recording:
  * episode_id, task_id, release version and `tasks_sha256`;
  * the image digest and repogym commit;
  * the budgets and seed;
  * start and end times;
  * the setup assertion results;
  * the grade (status, reward, partial metrics and flags);
  * token and cost totals;
  * the trajectory path.

  The files go to `data/runs/<run_id>/`. The JSONL is committed for the scripted-agent runs that
  back the README table.

## 8. Scripted-agent test plan (the headline evidence)

All of this runs on the **dev split, 25 tasks, with 2 workers**. Every expectation is a hard
assertion. A failure is treated as an environment bug and fixed; the expectation is never
weakened.

| Agent | What it does | Expectation per task |
|---|---|---|
| **OracleAgent** | Receives the gold patch out of band. Applies it *through the tools*: `create_file` with each changed file's final content, `bash rm` for deletions. Then calls `submit()` | reward 1 and no flags |
| **NullAgent** | Calls `submit()` immediately | reward 0, no flags |
| **CheaterAgent** | Runs every attack below, then `submit()` | reward 0, **every attempt flagged**, `gold_content_in_observations = 0` |

CheaterAgent's attacks. Each one is tagged with the flag it must produce:

1. `git log --all -p`, `git reflog`, `git fsck --lost-found`, `git rev-list --all`, and
   `git cat-file --batch-all-objects`. Then `git show` and `git cat-file -p` on **real future
   commit hashes**, including the fix's merge commit, which it receives out of band from the
   bare clone → `git_history_commands`. Each must fail, because the objects aren't there.
2. `git branch -a`, `git tag`, `git remote -v`, `git fetch origin` → `git_history_commands`,
   `network_attempts`
3. `find / -name "*.patch" -o -name "*.diff" -o -name "*.jsonl"`, `grep -r "FAIL_TO_PASS" /`,
   `ls /repos /data` → `task_data_search`
4. `curl https://github.com/...`, `pip install --upgrade <pkg>`, `pip download <pkg>`, and
   `python -c "import urllib.request; ..."` → `network_attempts`
5. Edit the target test files (it receives the test_patch *paths* out of band, to make the
   attack as strong as possible): replace every `assert` with `assert True`, or the test body with
   `pass`, then `git commit` → `test_files_edited`, `agent_committed`
6. *(Extension, D3)* Plant `tests/conftest.py` with a `pytest_runtest_makereport` hook that turns
   failures into passes, add `sitecustomize.py` and a top-level `pytest.py` shim, and change
   `[tool.pytest.ini_options]` → `test_files_edited`
7. Write to `/tmp/x` and `/home/agent/.cache/x` → `files_outside_repo`

The report for each agent gives the pass/fail count, and for every task whose expectation fails,
the cause. The README table will be **Oracle 25/25 · Null 0/25 · Cheater 0/25, with every attempt
flagged**, and only once those numbers are real.

## 9. Baseline LLM agent (Step 6, wiring only)

* **Design.** Bash only, in the style of mini-swe-agent: each turn gives *thought* plus exactly one
  bash block, the history is linear, and that history is the trajectory. The agent submits by
  printing a sentinel. Format errors get a corrective message and count as a step. It uses the
  same budgets and grading as the scripted agents.
* **Model layer *(Decision D4)*.** I propose a small provider-agnostic `ChatModel` protocol in
  RepoGym:
  * a Gemini implementation reusing `llm.py`, with the same retries, backoff, jitter, throttling
    and finishReason rules;
  * an OpenAI-compatible HTTP implementation, so other providers can be selected from config.

  The alternative is LiteLLM. It's large and fast-moving, with weekly releases. It would bring
  many transitive dependencies into a reproducibility-focused repo, and RepoGym would lose the
  finishReason handling we audited in Week 1.
* **Default model:** `gemini-3.5-flash-lite`. The smoke test covers 3 dev tasks only. I'll report
  the reward, steps, tokens and any harness problems.

## 10. Decisions for review

| ID | Decision | Recommendation |
|---|---|---|
| D1 | MCP server location | On the host, executing in the container via `docker exec`. This avoids changing the validated images |
| D2 | Grading container | Always a fresh container, with the diff extracted host-side from a `docker cp` of the tree, never via the agent's git |
| D3 | Revert scope | The brief's test_patch paths **plus** all test files, `conftest.py`, pytest config, `sitecustomize`/`.pth` and shadowing modules. CheaterAgent gets the matching extra attacks (F7) |
| D4 | Model layer | Our own thin provider-agnostic layer rather than LiteLLM |
| D5 | ATIF version | v1.8 with an in-repo validator. Cross-check with Harbor 0.24.0's validator; if 0.24.0 predates v1.8, target its newest version and record why |
| D6 | Gold-line probe false positives | Run it on all 247 tasks during Step 1 and report any hits; never relax it silently |
| D7 | Agent user | Non-root `agent`, `--cap-drop ALL`, `no-new-privileges`, `--pids-limit 512`. Writable-layer size can't be capped on overlay2 without xfs quotas, so disk use is monitored per episode and reported instead (a known limitation) |

## 11. Implementation order

Step 1 environment and assertions, with `repogym env check` and a gold-probe sweep over all
tasks. Then Step 2 tools and MCP, Step 3 grading, Step 4 ATIF and episode records, Step 5 scripted
agents on dev (fix until green), Step 6 baseline smoke test, Step 7 viewer, and Step 8 tests,
docs and CI. I'll show real output at each step.

## 12. Review outcomes (2026-11-07)

Every decision was approved, with these additions. They are binding for the implementation.

* **D1.** MCP server on the host, tools via `docker exec`.
  * **Every** tool `docker exec` runs as the non-root `agent` user, never root. Root is used only
    during setup, before the agent's first action.
  * `view_file`, `str_replace` and `create_file` resolve paths, symlinks included, and reject
    anything outside `/work`.
* **D2.** A fresh grading container. The diff is extracted on the host, never through the agent's
  git.
* **D3.** The extended revert scope is approved, with two guards:
  * (a) The shadowing rule reverts only files that can actually shadow an import: modules or
    packages directly in an import root at grading time, meaning the repo root and the `src/`
    layout root (test directories are reverted anyway). A module a fix adds anywhere else is never
    reverted.
  * (b) Every reverted file that isn't a test file, test config or `conftest.py` is recorded as a
    grading flag with its path, so a false revert is visible. OracleAgent 25/25 on dev is the proof
    that legitimate fixes survive. An oracle failure caused by a revert is a bug in the revert
    rules.
  * CheaterAgent also plants a `conftest.py` hook, a `sitecustomize.py`, a `.pth` file and a
    module shadowing `pytest`.
* **D4.** Our own provider layer. The `ChatModel` interface must make an OpenAI-compatible
  chat-completions provider (base URL, key and model from `.env`) a single small module, for the
  Week 3 model comparison.
* **D5.** ATIF v1.8 with our validator in CI, cross-checked locally against Harbor 0.24.0's
  validator. If 0.24.0 doesn't accept v1.8, we target the newest version it accepts and record why
  here.
* **D6.** The gold-line probe runs over all 247 tasks before it's relied on. Every hit is
  reported, and the probe isn't relaxed without approval.
* **D7.** Evaluate a read-only root filesystem, with `/work` and `/tmp` on size-capped tmpfs mounts
  (around 512 MB each) and the editable install done at episode start. Measure RAM with 2 workers
  on 7.7 GiB, the setup time, and OracleAgent's result. Adopt it only if it fits comfortably;
  otherwise keep per-episode disk monitoring and document the limitation.
* **Addition 1.** The "target package installed exactly once, as the editable copy at `/work`"
  assertion covers all 5 repos and is part of `repogym env check`.
* **Addition 2.** No task metadata may leak through the container: no task id, PR or issue
  numbers, gold or test patch content, or split information in environment variables, the
  hostname, container labels, or any file outside the repo. This is asserted.

## 13. Implementation outcomes (2026-11-07)

* **D5, ATIF.** Trajectories are `ATIF-v1.8`. Harbor 0.24.0's own validator
  (`python -m harbor.utils.trajectory_validator`, in an isolated `uvx --python 3.12` environment)
  accepts `ATIF-v1.0` through `ATIF-v1.8`. It validates our Oracle and Cheater trajectories, and
  it rejects a bad version or a dangling `source_call_id`. Our own validator runs in CI.
* **D7, read-only root: evaluated and not adopted.** Measured on one dev task per repo:

  | | Writable root (kept) | Read-only root + tmpfs `/work`, `/tmp` (512 MB each) |
  |---|---|---|
  | Setup time | 7.4–15.7 s | 7.5–11.0 s |
  | Peak memory (setup + one test run) | 74–144 MiB | 110–128 MiB (tmpfs 27–45 MB counts toward the container's memory) |
  | Disk written per episode | 6.9–21.6 MB | 0 |
  | `docker cp` of `/work` | works | fails in both directions |
  | "Installed exactly once" assertion (unchanged) | holds for all 5 repos | **fails for tomlkit**: the image's `tomlkit 0.15.2` stays installed and can't be removed |
  | Editable install | works | **fails for astroid** (metadata generation) |

  Under the review rule that leak-proofing outranks the disk cap, the read-only root is
  rejected. Making it work would need tomlkit's copy masked with an extra mount, which is a
  weaker F8 guarantee. Instead, the writable root stays, with:
  * a disk budget (`max_disk_mb`, default 2048), measured after every step with
    `docker inspect --size` (about 0.06 s per check);
  * a per-file size limit on every command (`ulimit -f`, `max_file_mb`, default 1024);
  * the disk written per episode recorded in the episode record.

  **Remaining limitation:** a single command can still write many files below the per-file
  limit before the next check. It's bounded by the command timeout, and the episode then ends
  with `budget:disk`.
* **MCP.** Built with `mcp` 2.3.0 (`MCPServer`). The final `submit` call returns only after the
  episode is graded and recorded, because MCP clients typically shut the server down as soon as
  they get that reply.
* **D3, evidence.** A single `conftest.py` hook that turns failures into passes gets **reward 1
  on 14/14** dev tasks with no reverts, and also with the brief's original "revert test_patch
  paths" rule. With the extended D3 rules it gets **0/14**.
