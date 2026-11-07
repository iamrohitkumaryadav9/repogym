"""The ``repogym`` command-line interface."""

from __future__ import annotations

from typing import Annotated

import typer

from repogym.config import CANDIDATES_PATH, MINING_REPORT_PATH, REPOS, get_repo
from repogym.logs import configure_logging

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)


def _select_repos(repos: list[str] | None) -> list[str]:
    return [get_repo(name).slug for name in repos] if repos else list(REPOS)


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


if __name__ == "__main__":
    app()
