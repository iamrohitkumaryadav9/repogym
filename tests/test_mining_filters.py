from repogym.mining.links import issues_from_timeline, parse_closing_references
from repogym.mining.miner import PrFacts, facts_from_split, primary_drop_reason
from repogym.mining.patches import SplitPatch

REPO = "pallets/click"


def test_closing_keywords_in_all_forms():
    body = (
        "fixes #1, Closes: #2, RESOLVED #3, fix https://github.com/pallets/click/issues/4, "
        "closes pallets/click#5"
    )
    assert parse_closing_references(body, REPO) == [1, 2, 3, 4, 5]


def test_bare_mentions_are_not_links():
    assert parse_closing_references("related to #1, see #2, prefixes #3", REPO) == []


def test_template_comments_and_code_are_ignored():
    body = "<!-- fixes #1 -->\n```\nfixes #2\n```\n`fixes #3`\nfixes #4"
    assert parse_closing_references(body, REPO) == [4]


def test_other_repositories_are_ignored():
    body = "fixes other/project#1, fixes https://github.com/other/project/issues/2"
    assert parse_closing_references(body, REPO) == []


def test_empty_body():
    assert parse_closing_references(None, REPO) == []


def test_duplicates_are_removed():
    assert parse_closing_references("fixes #1 and closes #1", REPO) == [1]


def _xref(number, closed_at="2024-01-01T12:00:00Z", state="closed", repo=REPO, pr=False):
    issue = {
        "number": number,
        "state": state,
        "closed_at": closed_at,
        "repository": {"full_name": repo},
    }
    if pr:
        issue["pull_request"] = {}
    return {"event": "cross-referenced", "source": {"issue": issue}}


def test_timeline_accepts_issue_closed_near_merge():
    assert issues_from_timeline([_xref(7)], REPO, "2024-01-01T12:30:00Z") == [7]


def test_timeline_rejects_weak_references():
    merged = "2024-01-01T12:00:00Z"
    events = [
        _xref(1, closed_at="2024-03-01T00:00:00Z"),  # closed long after the merge
        _xref(2, state="open", closed_at=None),
        _xref(3, repo="other/project"),
        _xref(4, pr=True),
        {"event": "labeled"},
    ]
    assert issues_from_timeline(events, REPO, merged) == []


def _link(event, number, state="closed", repo=REPO):
    return {
        "event": event,
        "subject": {"number": number, "state": state, "repository": {"full_name": repo}},
    }


def test_connected_events_link_closed_issues_in_this_repo():
    events = [_link("connected", 7), _link("connected", 8, state="open")]
    events.append(_link("connected", 9, repo="other/project"))
    assert issues_from_timeline(events, REPO, "2024-01-01T12:00:00Z") == [7]


def test_disconnected_cancels_an_earlier_connected():
    events = [_link("connected", 7), _link("disconnected", 7)]
    assert issues_from_timeline(events, REPO, "2024-01-01T12:00:00Z") == []


def test_reconnected_issue_counts():
    events = [_link("connected", 7), _link("disconnected", 7), _link("connected", 7)]
    assert issues_from_timeline(events, REPO, "2024-01-01T12:00:00Z") == [7]


GOOD = PrFacts(True, True, True, True, True)


def test_qualifying_pr_has_no_drop_reason():
    assert primary_drop_reason(GOOD) is None


def test_each_filter_is_reported():
    cases = {
        "no_linked_issue": PrFacts(False, True, True, True, True),
        "no_source_change": PrFacts(True, False, True, True, True),
        "no_test_change": PrFacts(True, True, False, True, True),
        "diff_too_large": PrFacts(True, True, True, False, True),
        "binary_change": PrFacts(True, True, True, True, False),
        "diff_unavailable": PrFacts(True, False, False, False, True, diff_available=False),
    }
    for expected, facts in cases.items():
        assert primary_drop_reason(facts) == expected


def _split(lines):
    return SplitPatch("g", "t", ["a.py"], ["tests/test_a.py"], [], lines, False)


def test_size_limit_is_exclusive():
    assert facts_from_split(True, _split(299), limit=300).within_size
    assert not facts_from_split(True, _split(300), limit=300).within_size


def test_missing_diff():
    assert primary_drop_reason(facts_from_split(True, None)) == "diff_unavailable"
