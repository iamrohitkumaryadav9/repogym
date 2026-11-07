"""The ``repogym`` command-line interface."""

from __future__ import annotations

import json
from typing import Annotated

import typer

from repogym.config import (
    BUILD_LOG_PATH,
    CANDIDATES_PATH,
    DEFAULT_WORKERS,
    DOCKER_BUDGET_BYTES,
    MINING_REPORT_PATH,
    REJECTIONS_PATH,
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
    write_jsonl_atomic(CANDIDATES_PATH, candidate_lines)
    write_jsonl_atomic(MINING_REPORT_PATH, report_lines)
    typer.echo(f"Wrote {len(candidate_lines)} candidates to {CANDIDATES_PATH}")


@app.command()
def build(
    repo: Annotated[list[str] | None, typer.Option(help="Repo slug; repeatable.")] = None,
) -> None:
    """Clone the repos, fetch every candidate's base commit and build one image per repo."""
    from datetime import UTC, datetime

    from repogym.env.docker import build_image, check_docker, disk_usage
    from repogym.env.repos import ensure_clones, ensure_commit
    from repogym.models import Candidate
    from repogym.validate import read_jsonl

    check_docker()
    candidates = read_jsonl(CANDIDATES_PATH, Candidate)
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


@app.command()
def validate(
    workers: Annotated[int, typer.Option(help="Parallel tasks.")] = DEFAULT_WORKERS,
    repo: Annotated[list[str] | None, typer.Option(help="Repo slug; repeatable.")] = None,
    retry: Annotated[
        list[str] | None, typer.Option(help="Rejection reason to re-validate; repeatable.")
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Only the first N per repo.")] = None,
) -> None:
    """Apply the validity checks. Safe to interrupt: reruns skip finished tasks."""
    from collections import Counter

    from repogym.env.docker import check_docker
    from repogym.models import Candidate
    from repogym.validate import forget_rejections, read_jsonl, validate_all

    check_docker()
    if retry:
        typer.echo(f"Forgot {forget_rejections(REJECTIONS_PATH, set(retry))} rejections")
    selected = {get_repo(slug).full_name for slug in _select_repos(repo)}
    candidates = [c for c in read_jsonl(CANDIDATES_PATH, Candidate) if c.repo in selected]
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
    typer.echo(f"\nMedian diff size (verified): {data.median_diff_lines} changed lines")
    typer.echo(f"Median test runtime (verified): {data.median_test_seconds} seconds")
    try:
        usage = disk_usage()
    except (DockerUnavailable, FileNotFoundError) as exc:
        typer.echo(f"\nDocker disk usage unavailable: {exc}")
        return
    typer.echo("\nDocker disk usage")
    typer.echo(f"  repogym images: {_gb(usage.repogym_bytes)} (upper bound; layers are shared)")
    budget = "OVER the" if usage.total_bytes > DOCKER_BUDGET_BYTES else "within the"
    typer.echo(f"  total, all projects: {_gb(usage.total_bytes)} ({budget} 10 GB budget)")
    for kind, size in usage.by_type.items():
        typer.echo(f"    {kind}: {_gb(size)}")


@app.command()
def clean(
    include_other_projects: Annotated[
        bool,
        typer.Option(help="Also remove stopped containers and dangling images of other projects."),
    ] = False,
) -> None:
    """Remove repogym's stopped containers, dangling images and images no verified task uses."""
    from repogym.env.docker import clean as docker_clean
    from repogym.models import VerifiedTask
    from repogym.validate import read_jsonl

    keep = {task.docker_image for task in read_jsonl(VERIFIED_PATH, VerifiedTask)}
    for kind, count in docker_clean(keep, other_projects=include_other_projects).items():
        typer.echo(f"Removed {count} {kind}")


if __name__ == "__main__":
    app()
