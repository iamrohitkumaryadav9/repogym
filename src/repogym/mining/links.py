"""Find the issue a pull request resolves."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`\n]*`")
_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"
_CLOSING_REF = re.compile(
    rf"\b{_KEYWORD}\b:?\s+"
    r"(?:(?P<owner>[\w.-]+)/(?P<name>[\w.-]+))?"
    r"(?:#|https?://github\.com/(?P<url_owner>[\w.-]+)/(?P<url_name>[\w.-]+)/issues/)"
    r"(?P<number>\d+)",
    re.IGNORECASE,
)

# A manual "development" link has no explicit payload on the REST timeline, so a mention only
# counts when the issue closed around the time the PR merged.
CROSS_REFERENCE_CLOSE_WINDOW = timedelta(days=1)


def _strip_markup(text: str) -> str:
    """Drop PR-template comments and code, where "fixes #123" is an example, not a link."""
    return _INLINE_CODE.sub("", _FENCE.sub("", _COMMENT.sub("", text)))


def parse_closing_references(text: str | None, repo: str) -> list[int]:
    """Return same-repo issue numbers named by GitHub closing keywords, in order, deduplicated."""
    if not text:
        return []
    numbers: list[int] = []
    for match in _CLOSING_REF.finditer(_strip_markup(text)):
        owner = match["owner"] or match["url_owner"]
        name = match["name"] or match["url_name"]
        if owner and f"{owner}/{name}".lower() != repo.lower():
            continue
        number = int(match["number"])
        if number not in numbers:
            numbers.append(number)
    return numbers


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def issues_from_timeline(events: list[dict[str, Any]], repo: str, merged_at: str) -> list[int]:
    """Return issues cross-referencing this PR that closed around its merge."""
    merged = _parse_time(merged_at)
    numbers: list[int] = []
    for event in events:
        if event.get("event") != "cross-referenced":
            continue
        issue = (event.get("source") or {}).get("issue") or {}
        if "pull_request" in issue or issue.get("state") != "closed":
            continue
        if not issue.get("closed_at"):
            continue
        source_repo = (issue.get("repository") or {}).get("full_name", "")
        if source_repo.lower() != repo.lower():
            continue
        if abs(_parse_time(issue["closed_at"]) - merged) <= CROSS_REFERENCE_CLOSE_WINDOW:
            if issue["number"] not in numbers:
                numbers.append(issue["number"])
    return numbers
