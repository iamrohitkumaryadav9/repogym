"""Turn merged pull requests into candidate tasks."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from repogym.config import MAX_CHANGED_LINES, MINING_WINDOW_DAYS, RepoSpec
from repogym.mining.github import DIFF_ACCEPT, GitHubClient
from repogym.mining.links import issues_from_timeline, parse_closing_references
from repogym.mining.patches import SplitPatch, split_patch
from repogym.models import Candidate

log = structlog.get_logger()


@dataclass(frozen=True)
class PrFacts:
    """Each filter evaluated independently, so overlaps between them can be reported."""

    has_issue: bool
    has_source: bool
    has_test: bool
    within_size: bool
    no_binary: bool
    diff_available: bool = True


# Order in which a dropped PR is attributed to one primary reason.
FILTER_ORDER = (
    "no_linked_issue",
    "diff_unavailable",
    "no_source_change",
    "no_test_change",
    "diff_too_large",
    "binary_change",
)


def primary_drop_reason(facts: PrFacts) -> str | None:
    """Return the first filter this PR fails, or None if it qualifies."""
    if not facts.has_issue:
        return "no_linked_issue"
    if not facts.diff_available:
        return "diff_unavailable"
    if not facts.has_source:
        return "no_source_change"
    if not facts.has_test:
        return "no_test_change"
    if not facts.within_size:
        return "diff_too_large"
    if not facts.no_binary:
        return "binary_change"
    return None


def facts_from_split(
    has_issue: bool, split: SplitPatch | None, limit: int = MAX_CHANGED_LINES
) -> PrFacts:
    if split is None:
        return PrFacts(has_issue, False, False, False, True, diff_available=False)
    return PrFacts(
        has_issue=has_issue,
        has_source=bool(split.source_files),
        has_test=bool(split.test_files),
        within_size=split.changed_lines < limit,
        no_binary=not split.has_binary,
    )


@dataclass
class RepoReport:
    repo: str
    merged_prs: int = 0
    closed_unmerged: int = 0
    candidates: int = 0
    primary_drops: Counter[str] = field(default_factory=Counter)
    independent_fails: Counter[str] = field(default_factory=Counter)


def iter_prs(client: GitHubClient, repo: str, since: datetime) -> Iterator[dict[str, Any]]:
    url = f"/repos/{repo}/pulls?state=closed&sort=created&direction=desc&per_page=100"
    for page in client.paginate(url):
        for pr in page:
            if datetime.fromisoformat(pr["created_at"].replace("Z", "+00:00")) < since:
                return
            yield pr


def _linked_issue_candidates(
    client: GitHubClient, repo: str, pr: dict[str, Any]
) -> tuple[list[int], str]:
    numbers = parse_closing_references(pr.get("body"), repo) + parse_closing_references(
        pr.get("title"), repo
    )
    if numbers:
        return list(dict.fromkeys(numbers)), "closing_keyword"
    if (pr.get("user") or {}).get("type") == "Bot":
        return [], "none"
    events: list[dict[str, Any]] = []
    for page in client.paginate(f"/repos/{repo}/issues/{pr['number']}/timeline?per_page=100"):
        events.extend(page)
    numbers = issues_from_timeline(events, repo, pr["merged_at"])
    return numbers, "timeline" if numbers else "none"


def _resolve_issue(client: GitHubClient, repo: str, numbers: list[int]) -> dict[str, Any] | None:
    """Return the first referenced number that is a real issue, not a PR or deleted."""
    for number in numbers:
        response = client.get(f"/repos/{repo}/issues/{number}")
        if response.status == 200:
            issue = response.json()
            if "pull_request" not in issue:
                return issue
    return None


def mine_repo(
    client: GitHubClient,
    spec: RepoSpec,
    now: datetime | None = None,
    window_days: int = MINING_WINDOW_DAYS,
) -> tuple[list[Candidate], RepoReport]:
    since = (now or datetime.now(UTC)) - timedelta(days=window_days)
    repo = spec.full_name
    report = RepoReport(repo=repo)
    candidates: list[Candidate] = []
    for index, pr in enumerate(iter_prs(client, repo, since), start=1):
        if not pr.get("merged_at"):
            report.closed_unmerged += 1
            continue
        report.merged_prs += 1
        if index % 100 == 0:
            log.info(
                "mining_progress",
                repo=repo,
                seen=index,
                candidates=len(candidates),
                requests=client.network_requests,
            )

        numbers, link_source = _linked_issue_candidates(client, repo, pr)
        issue = _resolve_issue(client, repo, numbers) if numbers else None

        diff = client.get(f"/repos/{repo}/pulls/{pr['number']}", accept=DIFF_ACCEPT)
        split = split_patch(diff.body, spec.source_roots) if diff.status == 200 else None
        facts = facts_from_split(issue is not None, split)

        for name, passed in (
            ("no_linked_issue", facts.has_issue),
            ("diff_unavailable", facts.diff_available),
            ("no_source_change", facts.has_source),
            ("no_test_change", facts.has_test),
            ("diff_too_large", facts.within_size),
            ("binary_change", facts.no_binary),
        ):
            if not passed:
                report.independent_fails[name] += 1

        reason = primary_drop_reason(facts)
        if reason is not None:
            report.primary_drops[reason] += 1
            continue

        assert issue is not None and split is not None
        report.candidates += 1
        candidates.append(
            Candidate(
                task_id=f"{repo.replace('/', '__')}-{pr['number']}",
                repo=repo,
                pr_number=pr["number"],
                issue_number=issue["number"],
                issue_title=issue["title"],
                issue_body=issue.get("body") or "",
                issue_labels=[label["name"] for label in issue.get("labels", [])],
                link_source=link_source,
                base_commit=pr["base"]["sha"],
                gold_patch=split.gold_patch,
                test_patch=split.test_patch,
                created_at=pr["created_at"],
                changed_lines=split.changed_lines,
                source_files=split.source_files,
                test_files=split.test_files,
                other_files=split.other_files,
            )
        )
    log.info(
        "repo_mined",
        repo=repo,
        merged=report.merged_prs,
        candidates=report.candidates,
        requests=client.network_requests,
        cache_hits=client.cache_hits,
    )
    return candidates, report


def write_jsonl_atomic(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    temp.replace(path)


def report_to_json(report: RepoReport) -> str:
    return json.dumps(
        {
            "repo": report.repo,
            "merged_prs": report.merged_prs,
            "closed_unmerged": report.closed_unmerged,
            "candidates": report.candidates,
            "primary_drops": dict(report.primary_drops),
            "independent_fails": dict(report.independent_fails),
        }
    )
