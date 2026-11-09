"""The RepoGym episode environment: reset(task_id) / step(action) / state(), OpenEnv-style.

Synchronous and in-process. The hidden task data (gold patch, test patch, test lists) stays in
this host-side object and is used only for the leakage checks' needles and, after the episode,
for grading (repogym.grade). Every tool call runs in the container as the unprivileged agent user.
"""

from __future__ import annotations

import json
import secrets
import shlex
import time
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from repogym.config import CACHE_DIR, RELEASE_DIR, get_repo
from repogym.env.budgets import Budgets, truncate
from repogym.env.extract import AgentWork, classify_outside, extract
from repogym.env.history import ProbeResult, distinctive_lines, git, probe_bundle
from repogym.env.sandbox import Check, Needles, Sandbox, SetupReport, submodule_pins
from repogym.env.sandbox_setup import MARKER, WORK
from repogym.release import ReleaseTask
from repogym.tools import file_ops
from repogym.tools.definitions import BY_NAME, TOOLS, validate_arguments

PER_TEST_TIMEOUT = 30
_FILE_OPS_SOURCE = open(file_ops.__file__, encoding="utf-8").read()  # noqa: SIM115


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Action(BaseModel):
    tool: str
    arguments: dict = Field(default_factory=dict)
    call_id: str | None = None
    # Agent-side context recorded into the trajectory (LLM agents); not seen by the tools.
    message: str = ""
    reasoning: str | None = None
    model_name: str | None = None
    metrics: dict | None = None
    llm_calls: int = 0


class Observation(BaseModel):
    text: str
    tool: str | None = None
    ok: bool = True
    exit_code: int | None = None
    truncated: bool = False
    done: bool = False


class StepRecord(BaseModel):
    step: int
    call_id: str
    started_at: str
    seconds: float
    tool: str | None  # None: an LLM turn with no valid action (the reply is the observation)
    arguments: dict
    observation: str
    ok: bool
    exit_code: int | None = None
    truncated: bool = False
    timed_out: bool = False
    raw_chars: int = 0
    message: str = ""
    reasoning: str | None = None
    model_name: str | None = None
    metrics: dict | None = None
    llm_calls: int = 0


class EventRecord(BaseModel):
    """Something the agent's harness did that isn't a tool call: a context-management event
    (clip, mask, summary, drop, fallback). Events never count against the step budget."""

    after_step: int  # the number of steps taken when it happened
    at: str
    kind: str
    detail: dict = Field(default_factory=dict)
    message: str = ""  # e.g. the summary text
    metrics: dict | None = None  # an LLM call's usage, for summary events


class EpisodeState(BaseModel):
    episode_id: str
    task_id: str
    step_count: int = 0
    done: bool = False
    done_reason: str | None = None  # submitted | budget:<name> | error
    started_at: str | None = None
    elapsed_seconds: float = 0.0
    total_observation_chars: int = 0
    disk_mb: float = 0.0  # container writable layer after the last step
    peak_disk_mb: float = 0.0
    budgets: Budgets


def load_task(task_id: str, version: str = "v0.1") -> ReleaseTask:
    with (RELEASE_DIR / version / "tasks.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            if f'"task_id":"{task_id}"' in line:
                return ReleaseTask.model_validate_json(line)
    raise KeyError(f"task {task_id} is not in release {version}")


def task_needles(task: ReleaseTask) -> Needles:
    spec = get_repo(task.repo)
    pr, issue = task.pr_url.rsplit("/", 1)[1], task.issue_url.rsplit("/", 1)[1]
    strings = [task.task_id, f"{task.repo}/pull/{pr}", f"{task.repo}/issues/{issue}"]
    strings += task.fail_to_pass
    strings += distinctive_lines(spec.bare_path, task.base_commit, task.gold_patch)
    strings += distinctive_lines(spec.bare_path, task.base_commit, task.test_patch)
    return Needles(tuple(dict.fromkeys(strings)), (pr, issue))


def gold_probe(task: ReleaseTask, sink: dict | None = None):
    """A bundle check, run on the exact bundle that goes into the container.

    Hard-fails only if the exact fix is reachable (a patch-id match): that would mean the
    history was built wrongly, since it holds base_commit's ancestors only. Overlap of the fix's
    distinctive lines with PAST commits is legitimate context a developer would have (D6), so it
    is recorded in ``sink["history_overlap"]``, not failed on. Cached per (task, base, gold)."""
    import dataclasses
    import hashlib

    spec = get_repo(task.repo)
    key = hashlib.sha256((task.base_commit + task.gold_patch).encode()).hexdigest()[:16]
    cache = CACHE_DIR / "probe" / f"{task.task_id}-{key}.json"

    def check(bundle) -> Check:
        if cache.exists():
            result = ProbeResult(**json.loads(cache.read_text()))
        else:
            lines = distinctive_lines(spec.bare_path, task.base_commit, task.gold_patch)
            result = probe_bundle(bundle, task.gold_patch, lines)
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(result.__dict__))
        overlap = result.overlap()
        if sink is not None:
            sink["history_overlap"] = dataclasses.asdict(overlap)
            # Distinctive gold lines that already exist in past history: legitimate context,
            # so the leak detector (flags.gold_content_in_observations) doesn't count them.
            sink["past_gold_lines"] = sorted(result.line_hits)
        if result.clean:
            detail = (
                f"no patch-id match in {result.commits} commits; past-history overlap "
                f"{overlap.lines}/{overlap.distinctive} distinctive lines, max "
                f"{overlap.max_fraction:.0%} in one commit, recoverable={overlap.recoverable}"
            )
        else:
            detail = f"the exact fix is reachable: patch-id matches {result.patch_id_match[:3]}"
        return Check("exact_fix_not_in_history", result.clean, detail)

    return check


def instructions(task: ReleaseTask, budgets: Budgets) -> str:
    return (
        "You are working in a Python repository at /work, checked out at the commit before an "
        "issue was fixed. Resolve the issue below by editing the source code. Hidden tests "
        "will be run against your changes after you submit; changes to test files are "
        "discarded before grading.\n\n"
        "Tools: " + ", ".join(t["name"] for t in TOOLS) + ". There is no network access. "
        f"Budgets: {budgets.max_steps} tool calls, {int(budgets.max_wall_seconds)} s wall-clock, "
        f"{int(budgets.command_timeout_seconds)} s per command; long outputs are truncated to "
        f"{budgets.max_observation_chars} characters. Call submit when you are done."
    )


def issue_text(task: ReleaseTask) -> str:
    return f"# {task.issue_title}\n\n{task.issue_body}".strip()


class Environment:
    def __init__(self, task: ReleaseTask, budgets: Budgets | None = None) -> None:
        self.task = task
        self.spec = get_repo(task.repo)
        self.budgets = budgets or Budgets()
        self.episode_id = secrets.token_hex(8)
        self._state = EpisodeState(
            episode_id=self.episode_id, task_id=task.task_id, budgets=self.budgets
        )
        self.steps: list[StepRecord] = []
        self.events: list[EventRecord] = []
        self.sandbox: Sandbox | None = None
        self.setup_report: SetupReport | None = None
        self._t0 = 0.0
        self._tracked: set[str] = set()
        self.probe_info: dict = {}  # history_overlap, filled in during setup

    # -- OpenEnv-style API -------------------------------------------------------------------

    def reset(self) -> Observation:
        self.sandbox = Sandbox(
            self.spec,
            self.task.base_commit,
            task_needles(self.task),
            bundle_checks=[gold_probe(self.task, self.probe_info)],
        )
        self.setup_report = self.sandbox.setup()  # raises EnvSetupError; no episode starts
        self._tracked = set(
            git(
                "--git-dir",
                str(self.spec.bare_path),
                "ls-tree",
                "-r",
                "--name-only",
                self.task.base_commit,
            ).splitlines()
        )
        self._t0 = time.monotonic()
        self._state.started_at = now()
        tools = "\n".join(f"- {t['name']}: {t['description']}" for t in TOOLS)
        text = (
            f"{instructions(self.task, self.budgets)}\n\nRepository: /work\n\n"
            f"Available tools:\n{tools}\n\n{issue_text(self.task)}"
        )
        return Observation(text=text)

    def state(self) -> EpisodeState:
        if self._t0:
            self._state.elapsed_seconds = round(time.monotonic() - self._t0, 2)
        return self._state.model_copy(deep=True)

    def step(self, action: Action) -> tuple[Observation, bool, dict]:
        if self.sandbox is None:
            raise RuntimeError("call reset() first")
        if self._state.done:
            return Observation(text="The episode is over.", ok=False, done=True), True, {}
        exhausted = self._exhausted()
        if exhausted:
            return self._finish(f"budget:{exhausted}"), True, {"done_reason": f"budget:{exhausted}"}
        self._state.step_count += 1
        call_id = action.call_id or f"call_{self._state.step_count}"
        started, t = now(), time.monotonic()
        error = validate_arguments(action.tool, action.arguments)
        timed_out, exit_code = False, None
        if error:
            ok, text = False, error
        elif action.tool == "submit":
            ok, text = True, "Submitted."
        else:
            ok, text, exit_code, timed_out = self._dispatch(action.tool, action.arguments)
        raw_chars = len(text)
        text, truncated = truncate(text, self.budgets.max_observation_chars)
        self._state.total_observation_chars += len(text)
        self.steps.append(
            StepRecord(
                step=self._state.step_count,
                call_id=call_id,
                started_at=started,
                seconds=round(time.monotonic() - t, 2),
                tool=action.tool,
                arguments=action.arguments,
                observation=text,
                ok=ok,
                exit_code=exit_code,
                truncated=truncated,
                timed_out=timed_out,
                raw_chars=raw_chars,
                message=action.message,
                reasoning=action.reasoning,
                model_name=action.model_name,
                metrics=action.metrics,
                llm_calls=action.llm_calls,
            )
        )
        observation = Observation(
            text=text, tool=action.tool, ok=ok, exit_code=exit_code, truncated=truncated
        )
        if hasattr(self.sandbox, "disk_bytes"):
            disk = round(self.sandbox.disk_bytes() / 1e6, 1)
            self._state.disk_mb, self._state.peak_disk_mb = (
                disk,
                max(disk, self._state.peak_disk_mb),
            )
        if action.tool == "submit" and not error:
            self._state.done, self._state.done_reason = True, "submitted"
        elif exhausted := self._exhausted(after_step=True):
            self._state.done, self._state.done_reason = True, f"budget:{exhausted}"
        observation.done = self._state.done
        return observation, self._state.done, {"done_reason": self._state.done_reason}

    def note(
        self,
        reply: str,
        message: str = "",
        model_name: str | None = None,
        metrics: dict | None = None,
        llm_calls: int = 1,
    ) -> tuple[bool, str | None]:
        """Record an agent turn that produced no tool call (e.g. a format error) and the reply
        the agent gets. It counts as a step against the budget. Returns (done, reason)."""
        if self._state.done:
            return True, self._state.done_reason
        if exhausted := self._exhausted():
            self._finish(f"budget:{exhausted}")
            return True, self._state.done_reason
        self._state.step_count += 1
        text, truncated = truncate(reply, self.budgets.max_observation_chars)
        self._state.total_observation_chars += len(text)
        self.steps.append(
            StepRecord(
                step=self._state.step_count,
                call_id=f"turn_{self._state.step_count}",
                started_at=now(),
                seconds=0.0,
                tool=None,
                arguments={},
                observation=text,
                ok=False,
                truncated=truncated,
                raw_chars=len(reply),
                message=message,
                model_name=model_name,
                metrics=metrics,
                llm_calls=llm_calls,
            )
        )
        if exhausted := self._exhausted(after_step=True):
            self._state.done, self._state.done_reason = True, f"budget:{exhausted}"
        return self._state.done, self._state.done_reason

    def record_event(
        self, kind: str, detail: dict | None = None, message: str = "", metrics: dict | None = None
    ) -> None:
        """Record a context event. It is not a step and doesn't touch the sandbox."""
        self.events.append(
            EventRecord(
                after_step=self._state.step_count,
                at=now(),
                kind=kind,
                detail=detail or {},
                message=message,
                metrics=metrics,
            )
        )

    def close(self) -> None:
        if self.sandbox is not None:
            self.sandbox.remove()

    def collect(self) -> AgentWork:
        """What the agent changed: the diff (host-side) and every path touched outside /work."""
        assert self.sandbox is not None and self.setup_report is not None
        tar = self.sandbox.copy_out(WORK)
        self._tar = tar
        gitlinks = {path for path, _ in submodule_pins(self.spec.bare_path, self.task.base_commit)}
        work = extract(
            tar,
            self.spec.bare_path,
            self.task.base_commit,
            self.setup_report.setup_artifacts,
            gitlinks,
        )
        delta = self.sandbox.changed_paths() - self.setup_report.baseline_changes
        scan = self.sandbox.exec_agent(
            [
                "find",
                "/",
                "-xdev",
                "(",
                "-path",
                "/proc",
                "-o",
                "-path",
                "/sys",
                "-o",
                "-path",
                "/dev",
                "-o",
                "-path",
                WORK,
                ")",
                "-prune",
                "-o",
                "-cnewer",
                MARKER,
                "-print",
            ],
            timeout=120,
        )
        paths = set(delta) | {p for p in scan.stdout.splitlines() if p.startswith("/")}
        # A directory is listed when something inside it changed; keep the deepest paths.
        leaves = [p for p in paths if not any(o.startswith(p.rstrip("/") + "/") for o in paths)]
        work.outside_repo, work.test_tmp = classify_outside(leaves)
        return work

    def read_agent_file(self, path: str) -> bytes | None:
        """A file as the agent left it, from the copied tree (for config comparisons)."""
        import io
        import tarfile

        if not getattr(self, "_tar", None):
            return None
        with tarfile.open(fileobj=io.BytesIO(self._tar), mode="r:") as tar:
            try:
                member = tar.getmember(f"work/{path}")
            except KeyError:
                return None
            handle = tar.extractfile(member) if member.isfile() else None
            return handle.read() if handle else None

    # -- internals ---------------------------------------------------------------------------

    def _remaining(self) -> float:
        return self.budgets.max_wall_seconds - (time.monotonic() - self._t0)

    def _exhausted(self, after_step: bool = False) -> str | None:
        if self._state.step_count >= self.budgets.max_steps and not after_step:
            return "steps"
        if self._remaining() <= 0:
            return "wall_clock"
        if self._state.total_observation_chars >= self.budgets.max_total_observation_chars:
            return "observation_chars"
        if self._state.disk_mb > self.budgets.max_disk_mb:
            return "disk"
        if after_step and self._state.step_count >= self.budgets.max_steps:
            return "steps"
        return None

    def _finish(self, reason: str) -> Observation:
        self._state.done, self._state.done_reason = True, reason
        return Observation(text=f"Episode ended: {reason} budget exhausted.", ok=False, done=True)

    def _timeout(self, limit: float) -> float:
        return max(1.0, min(limit, self._remaining()))

    def _dispatch(self, tool: str, args: dict) -> tuple[bool, str, int | None, bool]:
        assert self.sandbox is not None
        if tool == "bash":
            limit = self._timeout(self.budgets.command_timeout_seconds)
            fsize = self.budgets.max_file_mb * 1024  # ulimit -f counts 1 KiB blocks
            script = f"ulimit -f {fsize}\n{args['command']}"
            done = self.sandbox.exec_agent(["bash", "-lc", script], timeout=limit)
            return self._exec_text(done, limit)
        if tool == "run_tests":
            target = args["target"].strip()
            path = target.split("::", 1)[0]
            if path not in self._tracked:
                return (
                    False,
                    f"run_tests: {path!r} is not a test file in the repository at the "
                    "start of the task. run_tests only runs existing tests; use bash to run "
                    "tests you created (e.g. `python -m pytest path`).",
                    None,
                    False,
                )
            limit = self._timeout(self.budgets.run_tests_timeout_seconds)
            command = [
                "python",
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                f"--timeout={PER_TEST_TIMEOUT}",
                "--timeout-method=signal",
                target,
            ]
            done = self.sandbox.exec_agent(command, timeout=limit)
            return self._exec_text(done, limit)
        request = {"op": tool, **args}
        done = self.sandbox.exec_agent(
            ["python3", "-c", _FILE_OPS_SOURCE],
            timeout=self._timeout(60),
            stdin=json.dumps(request).encode(),
        )
        try:
            reply = json.loads(done.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError):
            return (
                False,
                f"{tool}: internal error: {(done.stdout + done.stderr)[-500:]}",
                None,
                False,
            )
        if reply["ok"]:
            return True, reply["output"], None, False
        return False, f"{tool}: {reply['error']}", None, False

    @staticmethod
    def _exec_text(done, limit: float) -> tuple[bool, str, int | None, bool]:
        parts = []
        if done.timed_out:
            parts.append(f"[command killed after {int(limit)} s timeout]")
        else:
            parts.append(f"[exit code {done.exit_code}]")
        if done.stdout:
            parts.append(done.stdout.rstrip("\n"))
        if done.stderr:
            parts.append("[stderr]\n" + done.stderr.rstrip("\n"))
        return (
            not done.timed_out and done.exit_code == 0,
            "\n".join(parts),
            done.exit_code,
            done.timed_out,
        )


def quote(command: list[str]) -> str:
    return " ".join(shlex.quote(c) for c in command)


def tool_names() -> list[str]:
    return list(BY_NAME)
