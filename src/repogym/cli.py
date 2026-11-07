"""The ``repogym`` command-line interface."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from repogym.config import (
    BUILD_LOG_PATH,
    CANDIDATES_PATH,
    DEFAULT_WORKERS,
    DOCKER_BUDGET_BYTES,
    MINING_REPORT_PATH,
    REJECTIONS_PATH,
    RELEASE_DIR,
    REPOS,
    VERIFIED_PATH,
    get_repo,
)
from repogym.logs import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)


def _select_repos(repos: list[str] | None) -> list[str]:
    return [get_repo(name).slug for name in repos] if repos else list(REPOS)


def _gb(size: int) -> str:
    return f"{size / 1000**3:.2f} GB"


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    configure_logging(verbose)


@app.command()
def mine(
    repo: Annotated[list[str] | None, typer.Option(help="Repo slug; repeatable.")] = None,
    refresh: Annotated[bool, typer.Option(help="Ignore the API response cache.")] = False,
    out: Annotated[
        Path | None,
        typer.Option(help="Write candidates here instead of data/ (report goes next to it)."),
    ] = None,
) -> None:
    """Fetch merged PRs and write data/tasks_candidates.jsonl."""
    from repogym.mining.github import GitHubClient, load_token
    from repogym.mining.miner import mine_repo, report_to_json, write_jsonl_atomic

    client = GitHubClient(load_token(), refresh=refresh)
    candidate_lines: list[str] = []
    report_lines: list[str] = []
    try:
        for slug in _select_repos(repo):
            candidates, report = mine_repo(client, get_repo(slug))
            candidate_lines += [c.model_dump_json() for c in candidates]
            report_lines.append(report_to_json(report))
    finally:
        client.close()
    candidates_path = out or CANDIDATES_PATH
    report_path = out.with_name("mining_report.jsonl") if out else MINING_REPORT_PATH
    candidates_path.parent.mkdir(parents=True, exist_ok=True)
    write_jsonl_atomic(candidates_path, candidate_lines)
    write_jsonl_atomic(report_path, report_lines)
    typer.echo(f"Wrote {len(candidate_lines)} candidates to {candidates_path}")
    typer.echo(f"{client.network_requests} GitHub API requests, {client.cache_hits} from the cache")


@app.command()
def build(
    repo: Annotated[list[str] | None, typer.Option(help="Repo slug; repeatable.")] = None,
    check: Annotated[
        bool, typer.Option(help="Only check images, clones and base commits; build nothing.")
    ] = False,
) -> None:
    """Clone the repos, fetch every candidate's base commit and build one image per repo."""
    from datetime import UTC, datetime

    from repogym.env.docker import build_image, check_docker, disk_usage, ensure_builder
    from repogym.env.repos import ensure_clones, ensure_commit
    from repogym.models import Candidate
    from repogym.validate import read_jsonl

    check_docker()
    candidates = read_jsonl(CANDIDATES_PATH, Candidate)
    if check:
        from repogym.env.docker import image_id
        from repogym.env.repos import has_commit

        problems = 0
        for slug in _select_repos(repo):
            spec = get_repo(slug)
            commits = {c.base_commit for c in candidates if c.repo == spec.full_name}
            absent = [sha for sha in commits if not has_commit(spec, sha)]
            digest = image_id(spec.image)
            problems += (not digest) + len(absent)
            present = len(commits) - len(absent)
            typer.echo(
                f"{spec.image}: {digest[:19] or 'MISSING'}; "
                f"{present}/{len(commits)} base commits present"
            )
        if problems:
            raise typer.Exit(1)
        return
    ensure_builder()
    BUILD_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    for slug in _select_repos(repo):
        spec = get_repo(slug)
        ensure_clones(spec)
        commits = {c.base_commit for c in candidates if c.repo == spec.full_name}
        missing = [sha for sha in sorted(commits) if not ensure_commit(spec, sha)]
        build_image(spec)
        with BUILD_LOG_PATH.open("a", encoding="utf-8") as handle:
            entry = {
                "image": spec.image,
                "kind": "base",
                "reason": "one base image per repo",
                "built_at": datetime.now(UTC).isoformat(),
            }
            handle.write(json.dumps(entry) + "\n")
        typer.echo(f"{spec.image}: built; {len(commits)} base commits, {len(missing)} unavailable")
    usage = disk_usage()
    typer.echo(f"Docker disk: repogym {_gb(usage.repogym_bytes)}, total {_gb(usage.total_bytes)}")
    if usage.repogym_bytes > DOCKER_BUDGET_BYTES:
        typer.echo("WARNING: repogym is over its 10 GB budget; try `repogym clean --build-cache`")


@app.command()
def validate(
    workers: Annotated[int, typer.Option(help="Parallel tasks.")] = DEFAULT_WORKERS,
    repo: Annotated[list[str] | None, typer.Option(help="Repo slug; repeatable.")] = None,
    retry: Annotated[
        list[str] | None, typer.Option(help="Rejection reason to re-validate; repeatable.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Only the first N per repo.")] = None,
    task: Annotated[
        list[str] | None,
        typer.Option(
            help="Re-validate this task from scratch, discarding its records; repeatable."
        ),
    ] = None,
    all_tasks: Annotated[
        bool, typer.Option("--all", help="Re-validate every candidate from scratch.")
    ] = False,
    scratch: Annotated[
        Path | None,
        typer.Option(help="Write every result to this directory instead of data/ (a dry run)."),
    ] = None,
) -> None:
    """Apply the validity checks. Safe to interrupt: reruns skip finished tasks."""
    from collections import Counter

    import repogym.validate as v
    from repogym.env.docker import check_docker
    from repogym.models import Candidate
    from repogym.validate import forget_rejections, forget_tasks, read_jsonl, validate_all

    rejections_path = REJECTIONS_PATH
    if scratch is not None:
        scratch.mkdir(parents=True, exist_ok=True)
        for name in ("VERIFIED_PATH", "REJECTIONS_PATH", "BASELINE_PATH", "RUN_METRICS_PATH",
                     "PROVENANCE_PATH"):  # fmt: skip
            setattr(v, name, scratch / getattr(v, name).name)
        rejections_path = v.REJECTIONS_PATH
        typer.echo(f"Scratch run: results go to {scratch}, data/ is not touched")
    check_docker()
    if all_tasks:
        task = [c.task_id for c in read_jsonl(CANDIDATES_PATH, Candidate)]
    if task:
        typer.echo(f"Discarded {forget_tasks(set(task))} records for {len(task)} tasks")
    if retry:
        typer.echo(f"Forgot {forget_rejections(rejections_path, set(retry))} rejections")
    selected = {get_repo(slug).full_name for slug in _select_repos(repo)}
    candidates = [c for c in read_jsonl(CANDIDATES_PATH, Candidate) if c.repo in selected]
    if task:
        candidates = [c for c in candidates if c.task_id in set(task)]
    if limit is not None:
        seen: Counter[str] = Counter()
        limited = []
        for candidate in candidates:
            if seen[candidate.repo] < limit:
                limited.append(candidate)
                seen[candidate.repo] += 1
        candidates = limited
    tally = validate_all(candidates, workers)
    for reason, count in tally.most_common():
        typer.echo(f"{count:5d}  {reason}")


@app.command()
def annotate() -> None:
    """Recompute task_type and the spec check for every verified task, in place."""
    from collections import Counter

    from repogym.models import Candidate, VerifiedTask
    from repogym.validate import annotate as annotate_task
    from repogym.validate import read_jsonl

    candidates = {c.task_id: c for c in read_jsonl(CANDIDATES_PATH, Candidate)}
    tasks = read_jsonl(VERIFIED_PATH, VerifiedTask)
    tally: Counter[str] = Counter()
    for task in tasks:
        task_type, spec_clear, spec_issues = annotate_task(candidates[task.task_id])
        task.task_type, task.spec_clear, task.spec_issues = task_type, spec_clear, spec_issues
        tally[f"{task_type} spec_clear={spec_clear}"] += 1
    temp = VERIFIED_PATH.with_suffix(".tmp")
    temp.write_text("".join(t.model_dump_json() + "\n" for t in tasks), encoding="utf-8")
    temp.replace(VERIFIED_PATH)
    for key, count in sorted(tally.items()):
        typer.echo(f"{count:5d}  {key}")


def _print_counter(title: str, counts: dict[str, int]) -> None:
    typer.echo(f"\n{title}")
    if not counts:
        typer.echo("  (none)")
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0])):
        typer.echo(f"  {count:5d}  {key}")


@app.command()
def stats() -> None:
    """Print mining, validation and Docker disk statistics."""
    from repogym.env.docker import DockerUnavailable, disk_usage
    from repogym.stats import collect

    data = collect()
    _print_counter("Candidates mined per repo", data.candidates_per_repo)
    if MINING_REPORT_PATH.exists():
        typer.echo("\nMining drops per repo (first failing filter)")
        for line in MINING_REPORT_PATH.read_text(encoding="utf-8").splitlines():
            report = json.loads(line)
            typer.echo(
                f"  {report['repo']}: merged={report['merged_prs']} {report['primary_drops']}"
            )
    _print_counter("Rejection reasons", data.rejection_reasons)
    _print_counter("Rejections per repo", data.rejections_per_repo)
    _print_counter("Verified tasks per repo", data.verified_per_repo)
    typer.echo(f"\nTotal verified: {sum(data.verified_per_repo.values())}")
    _print_counter("Tasks with baseline failures, per repo", data.tasks_with_baseline_failures)
    _print_counter(
        "Most common baseline failures", dict(data.baseline_failing_tests.most_common(10))
    )
    typer.echo("\nVerified tasks by repo x task_type x spec_clear")
    typer.echo(f"  {'repo':24}{'type':>9}{'clear':>8}{'unclear':>9}")
    for (repo, task_type), cell in sorted(data.breakdown.items()):
        typer.echo(f"  {repo:24}{task_type:>9}{cell[True]:>8}{cell[False]:>9}")
    typer.echo("\nFAIL_TO_PASS tests per verified task")
    for bucket, count in data.fail_to_pass_buckets.items():
        typer.echo(f"  {bucket:>4}: {count}")
    typer.echo(f"\nMedian diff size (verified): {data.median_diff_lines} changed lines")
    typer.echo(f"Median test runtime (verified): {data.median_test_seconds} seconds")
    try:
        usage = disk_usage()
    except (DockerUnavailable, FileNotFoundError) as exc:
        typer.echo(f"\nDocker disk usage unavailable: {exc}")
        return
    budget = "OVER" if usage.repogym_bytes > DOCKER_BUDGET_BYTES else "within"
    typer.echo(
        f"\nRepoGym-only Docker disk: {_gb(usage.repogym_bytes)} ({budget} the 10 GB budget)"
    )
    typer.echo(f"  images:              {_gb(usage.repogym_image_bytes)}")
    typer.echo(f"  builder build cache: {_gb(usage.repogym_build_cache_bytes)}")
    typer.echo(f"  builder (buildkit):  {_gb(usage.builder_image_bytes)}")
    typer.echo(f"Total Docker disk, all projects: {_gb(usage.total_bytes)}")
    for kind, size in usage.by_type.items():
        typer.echo(f"    {kind}: {_gb(size)}")


@app.command()
def clean(
    include_other_projects: Annotated[
        bool,
        typer.Option(help="Also remove stopped containers and dangling images of other projects."),
    ] = False,
    build_cache: Annotated[
        bool, typer.Option(help="Also prune the repogym-builder build cache (only that builder).")
    ] = False,
) -> None:
    """Remove repogym's stopped containers, dangling images and images no verified task uses."""
    from repogym.env.docker import clean as docker_clean
    from repogym.env.docker import prune_build_cache
    from repogym.models import VerifiedTask
    from repogym.validate import read_jsonl

    keep = {task.docker_image for task in read_jsonl(VERIFIED_PATH, VerifiedTask)}
    for kind, count in docker_clean(keep, other_projects=include_other_projects).items():
        typer.echo(f"Removed {count} {kind}")
    if build_cache:
        typer.echo(f"repogym-builder cache: {prune_build_cache()}")


def _llm_client(interval: float, model: str | None = None):
    from repogym.llm import GeminiClient, MissingSettings, Settings

    try:
        settings = Settings.from_env()
    except MissingSettings as exc:
        typer.echo(f"{exc}. Add them to .env and rerun.")
        raise typer.Exit(1) from exc
    if model:  # a comparison run: that model only, no fallback
        settings = Settings(api_key=settings.api_key, model=model, fallback_model=model)
    return GeminiClient(settings, interval=interval)


@app.command()
def classify(
    task: Annotated[list[str] | None, typer.Option(help="Only these tasks; repeatable.")] = None,
    interval: Annotated[float, typer.Option(help="Seconds between API requests.")] = 5.0,
    model: Annotated[
        str | None, typer.Option(help="Comparison run with this model only (no fallback).")
    ] = None,
    out: Annotated[Path | None, typer.Option(help="Labels file (default: official).")] = None,
    refresh: Annotated[
        bool, typer.Option(help="Ignore cached answers; ask the API again.")
    ] = False,
) -> None:
    """Label verified tasks with the LLM classifier. Writes the labels file only.

    Labels for the given tasks replace earlier ones; labels for other tasks are kept.
    """
    from collections import Counter

    from repogym.classify import TaskTypeLabel
    from repogym.classify import classify as classify_task
    from repogym.config import TASK_TYPE_LABELS_PATH
    from repogym.models import Candidate, VerifiedTask
    from repogym.validate import read_jsonl

    path = out or TASK_TYPE_LABELS_PATH
    client = _llm_client(interval, model)
    candidates = {c.task_id: c for c in read_jsonl(CANDIDATES_PATH, Candidate)}
    ids = task or [t.task_id for t in read_jsonl(VERIFIED_PATH, VerifiedTask)]
    labels = {label.task_id: label for label in read_jsonl(path, TaskTypeLabel)}
    new = []
    try:
        for index, task_id in enumerate(ids, start=1):
            new.append(classify_task(candidates[task_id], client, refresh=refresh))
            if index % 25 == 0:
                typer.echo(f"{index}/{len(ids)} labelled, {client.requests} API requests")
    finally:
        client.close()
        labels.update({label.task_id: label for label in new})
        temp = path.with_suffix(".tmp")
        temp.write_text("".join(labels[t].model_dump_json() + "\n" for t in sorted(labels)),
                        encoding="utf-8")  # fmt: skip
        temp.replace(path)
    for (m, task_type), count in sorted(Counter((x.model, x.task_type) for x in new).items(),
                                        key=str):  # fmt: skip
        typer.echo(f"{count:5d}  {task_type:8} via {m}")
    typer.echo(f"{client.requests} API requests; {len(new)} labels written to {path}")


@app.command("classify-consensus")
def classify_consensus(
    interval: Annotated[float, typer.Option(help="Seconds between API requests.")] = 6.0,
    limit: Annotated[int | None, typer.Option(help="Only the first N unknown tasks.")] = None,
    report_only: Annotated[
        bool, typer.Option(help="Summarise the saved runs; make no API requests.")
    ] = False,
) -> None:
    """Three runs per task with no GitHub type label; resumable. Writes the runs file only."""
    from repogym.classify import TaskTypeLabel
    from repogym.config import TASK_TYPE_RUNS_PATH
    from repogym.consensus import classify_runs, decide, latest_runs, summarize
    from repogym.models import Candidate, VerifiedTask
    from repogym.validate import JsonlStore, read_jsonl

    candidates = {c.task_id: c for c in read_jsonl(CANDIDATES_PATH, Candidate)}
    unknown = sorted(t.task_id for t in read_jsonl(VERIFIED_PATH, VerifiedTask)
                     if t.task_type == "unknown")  # fmt: skip
    ids = unknown[:limit] if limit else unknown
    done = latest_runs(read_jsonl(TASK_TYPE_RUNS_PATH, TaskTypeLabel))
    if report_only:
        for line in summarize([decide(t, list(done.get(t, {}).values())) for t in ids]):
            typer.echo(line)
        return
    store = JsonlStore(TASK_TYPE_RUNS_PATH)
    client = _llm_client(interval)
    try:
        finished = classify_runs([candidates[t] for t in ids], client, done, store.append)
    finally:
        client.close()
    if not finished:
        typer.echo("Stopped: the API kept refusing (likely a quota). Rerun later to resume.")
    results = [decide(t, list(done.get(t, {}).values())) for t in ids]
    for line in summarize(results):
        typer.echo(line)
    typer.echo(f"{client.requests} API requests this session; runs in {TASK_TYPE_RUNS_PATH}")


env_app = typer.Typer(help="Episode environment commands.")
app.add_typer(env_app, name="env")


@env_app.command("check")
def env_check(task_id: Annotated[str, typer.Argument(help="Task id from the release")]) -> None:
    """Set up an episode for a task and run every leakage assertion, then tear it down."""
    from repogym.env.environment import Environment, load_task
    from repogym.env.sandbox import EnvSetupError

    env = Environment(load_task(task_id))
    try:
        env.reset()
    except EnvSetupError as exc:
        typer.echo(f"FAILED: {task_id}\n{exc}")
        raise typer.Exit(1) from exc
    finally:
        env.close()
    assert env.setup_report is not None
    for check in env.setup_report.checks:
        typer.echo(f"  {'PASS' if check.ok else 'FAIL'}  {check.name:28} {check.detail[:110]}")
    typer.echo(
        f"OK: {task_id}, {len(env.setup_report.checks)} checks passed in "
        f"{env.setup_report.seconds} s; container removed"
    )


@app.command()
def run(
    agent: Annotated[str, typer.Option(help="oracle | null | cheater | baseline")],
    split: Annotated[str, typer.Option(help="Release split (primary per-repo split).")] = "dev",
    tasks: Annotated[list[str] | None, typer.Option("--tasks", help="Only these task ids.")] = None,
    workers: Annotated[int, typer.Option(help="Parallel episodes.")] = 2,
    run_id: Annotated[str | None, typer.Option(help="Resume or name a run.")] = None,
    release: Annotated[str, typer.Option(help="Release version.")] = "v0.1",
) -> None:
    """Run an agent on a split; writes data/runs/<run_id>/ (episodes.jsonl + ATIF trajectories)."""
    from datetime import UTC, datetime

    from repogym.agents.scripted import CheaterAgent, NullAgent, OracleAgent
    from repogym.env.budgets import Budgets
    from repogym.harness import RUNS_DIR, run_many
    from repogym.release import ReleaseTask

    path = RELEASE_DIR / release / "tasks.jsonl"
    selected = [ReleaseTask.model_validate_json(line) for line in path.read_text().splitlines()]
    selected = [t for t in selected if (t.task_id in tasks if tasks else t.split == split)]
    factories = {"oracle": OracleAgent, "null": NullAgent, "cheater": lambda t: CheaterAgent(t)}
    if agent == "baseline":
        from repogym.agents.baseline import make_baseline

        factories["baseline"] = make_baseline()
    if agent not in factories:
        typer.echo(f"unknown agent {agent!r}")
        raise typer.Exit(1)
    run_id = run_id or f"{agent}-{split}-{datetime.now(UTC):%Y%m%dT%H%M%S}"
    run_dir = RUNS_DIR / run_id
    typer.echo(f"run {run_id}: {len(selected)} tasks, {workers} workers -> {run_dir}")

    def show(record) -> None:
        typer.echo(
            f"  {record.task_id:34} {record.status:12} reward={record.reward} "
            f"steps={record.steps} flags={','.join(record.flags_raised) or '-'}"
        )

    run_many(
        selected,
        factories[agent],
        run_dir,
        Budgets(),
        workers=workers,
        is_oracle=agent == "oracle",
        on_done=show,
    )
    if agent in ("oracle", "null", "cheater"):
        from repogym.expectations import summarise

        ok, problems = summarise(run_dir, agent, {t.task_id: t for t in selected})
        typer.echo(f"{agent}: {ok}/{len(selected)} episodes met every expectation")
        for problem in problems:
            typer.echo(f"  EXPECTATION FAILED  {problem}")


@app.command()
def view(port: Annotated[int, typer.Option(help="Port on 127.0.0.1.")] = 8765) -> None:
    """Browse runs and replay trajectories in a local web viewer."""
    from repogym.viewer import serve

    server = serve(port)
    typer.echo(f"RepoGym viewer on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


@app.command()
def mcp(
    task: Annotated[str, typer.Option(help="Task id from the release.")],
    run_id: Annotated[str, typer.Option(help="Run directory under data/runs/.")] = "mcp",
) -> None:
    """Serve one task's tools over MCP (stdio) for any MCP-capable agent; graded on submit."""
    from repogym.env.budgets import Budgets
    from repogym.harness import RUNS_DIR
    from repogym.tools.mcp_server import serve_task

    serve_task(task, RUNS_DIR / run_id, Budgets())


@app.command("llm-audit")
def llm_audit(
    delete_bad: Annotated[bool, typer.Option(help="Delete entries that fail the audit.")] = False,
) -> None:
    """Check every cached LLM answer is complete, schema-valid JSON (finishReason STOP)."""
    from repogym.classify import parse_label

    client = _llm_client(interval=0)
    total, problems = client.audit_cache(parse_label)
    client.close()
    typer.echo(f"{total} cache entries, {len(problems)} bad")
    for problem in problems:
        typer.echo(f"  {problem.path.name}  {problem.model}  {problem.problem}")
        if delete_bad:
            problem.path.unlink()
    if problems and delete_bad:
        typer.echo("Deleted. Rerun classify for the affected tasks.")


@app.command()
def sheets() -> None:
    """Write the blind task-type (30) and spec-clarity (25) labelling sheets."""
    from repogym.config import LABELLING_DIR, SPEC_SHEET, TASK_TYPE_SHEET
    from repogym.models import Candidate, VerifiedTask
    from repogym.sheets import (
        select_spec_sample,
        select_task_type_sample,
        write_spec_sheet,
        write_task_type_sheet,
    )
    from repogym.validate import read_jsonl

    LABELLING_DIR.mkdir(parents=True, exist_ok=True)
    for path in (TASK_TYPE_SHEET, SPEC_SHEET):
        if path.exists():
            typer.echo(f"{path} exists; not overwriting a sheet that may hold your labels")
            raise typer.Exit(1)
    candidates = {c.task_id: c for c in read_jsonl(CANDIDATES_PATH, Candidate)}
    tasks = read_jsonl(VERIFIED_PATH, VerifiedTask)
    type_sample = select_task_type_sample(tasks, candidates)
    write_task_type_sheet(TASK_TYPE_SHEET, type_sample, candidates, seed=11)
    spec_sample = select_spec_sample(tasks)
    write_spec_sheet(SPEC_SHEET, spec_sample, {t.task_id: t for t in tasks}, candidates, seed=12)
    typer.echo(f"{TASK_TYPE_SHEET}: {len(type_sample)} tasks")
    typer.echo(f"{SPEC_SHEET}: {len(spec_sample)} tasks")


@app.command()
def calibrate() -> None:
    """Score the classifier and the spec check against the filled-in labelling sheets."""
    from repogym.calibrate_report import report

    for line in report():
        typer.echo(line)


@app.command()
def release(
    version: Annotated[str, typer.Argument(help="Release name, e.g. v0.1")],
    dry_run: Annotated[
        bool, typer.Option(help="Build and validate in a temp dir; write nothing to data/release.")
    ] = False,
) -> None:
    """Freeze verified tasks into data/release/<version>/ with time-based splits."""
    from repogym.release_build import ReleaseError, build_release

    try:
        manifest, problems = build_release(version, dry_run=dry_run)
    except (ReleaseError, FileExistsError) as exc:
        typer.echo(f"Release refused: {exc}")
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(manifest, indent=2))
    if dry_run:
        status = "valid" if not problems else f"{len(problems)} problems: {problems}"
        typer.echo(f"Dry run: built in a temp dir and removed; release {status}. Nothing written.")
        if problems:
            raise typer.Exit(1)


if __name__ == "__main__":
    app()
