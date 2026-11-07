"""Bundles hold only base_commit's ancestors; the probe finds the gold patch in any history."""

import subprocess

import pytest

from repogym.env.history import (
    bundle_commits,
    commit_set_digest,
    distinctive_lines,
    make_bundle,
    patch_id,
    probe_bundle,
)

FIX_LINE = "    return compute_the_correct_answer(value, strict=True)"


def run(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True,
        env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
             "GIT_COMMITTER_EMAIL": "t@t", "HOME": str(cwd), "PATH": "/usr/bin:/bin"},
    ).stdout.strip()  # fmt: skip


def commit(repo, path, text, message):
    (repo / path).write_text(text)
    run(repo, "add", path)
    run(repo, "commit", "-q", "-m", message)
    return run(repo, "rev-parse", "HEAD")


@pytest.fixture
def upstream(tmp_path):
    """A -- B (base) -- C (the fix), a feature branch off B with its own commit, a tag on C,
    reflog entries from a reset, all in a bare clone like data/repos/*.git."""
    work = tmp_path / "work"
    work.mkdir()
    run(work, "init", "-q", "-b", "main")
    a = commit(work, "lib.py", "def f(value):\n    return value\n", "A")
    b = commit(work, "lib.py", "def f(value):\n    return value + 0\n", "B base")
    c = commit(work, "lib.py", f"def f(value):\n{FIX_LINE}\n", "C fix")
    run(work, "tag", "v1.0", c)
    run(work, "checkout", "-q", "-b", "feature", b)
    d = commit(work, "other.py", "x = 'feature branch work'\n", "D feature")
    run(work, "checkout", "-q", "main")
    run(work, "reset", "-q", "--hard", b)  # reflog now remembers C
    run(work, "reset", "-q", "--hard", c)
    bare = tmp_path / "bare.git"
    run(tmp_path, "clone", "-q", "--bare", str(work), str(bare))
    gold = run(work, "diff", b, c)
    return {"bare": bare, "a": a, "b": b, "c": c, "d": d, "gold": gold + "\n"}


def test_bundle_holds_exactly_the_base_and_its_ancestors(upstream, tmp_path):
    bundle = make_bundle(upstream["bare"], upstream["b"], tmp_path / "t.bundle")
    assert bundle_commits(bundle) == sorted([upstream["a"], upstream["b"]])
    heads = run(tmp_path, "bundle", "list-heads", str(bundle))
    assert heads.split() == [upstream["b"], "refs/heads/repogym"]  # one ref, no tags or branches


def test_bundle_leaves_no_temporary_ref_behind(upstream, tmp_path):
    make_bundle(upstream["bare"], upstream["b"], tmp_path / "t.bundle")
    refs = run(upstream["bare"], "for-each-ref", "--format=%(refname)")
    assert "repogym" not in refs


def test_distinctive_lines_skip_lines_already_at_base(upstream):
    lines = distinctive_lines(upstream["bare"], upstream["b"], upstream["gold"])
    assert lines == [FIX_LINE.strip()]
    patch = upstream["gold"] + "+def f(value):\n"  # short and already present: never distinctive
    assert distinctive_lines(upstream["bare"], upstream["b"], patch) == [FIX_LINE.strip()]


def test_probe_is_clean_for_a_base_only_bundle(upstream, tmp_path):
    bundle = make_bundle(upstream["bare"], upstream["b"], tmp_path / "t.bundle")
    lines = distinctive_lines(upstream["bare"], upstream["b"], upstream["gold"])
    result = probe_bundle(bundle, upstream["gold"], lines)
    assert result.clean and result.commits == 2
    assert result.commit_set_sha256 == commit_set_digest([upstream["a"], upstream["b"]])


def test_probe_finds_the_fix_when_history_contains_it(upstream, tmp_path):
    # A bundle at the fix commit stands in for a leaky history (future commit reachable).
    bundle = make_bundle(upstream["bare"], upstream["c"], tmp_path / "leaky.bundle")
    lines = distinctive_lines(upstream["bare"], upstream["b"], upstream["gold"])
    result = probe_bundle(bundle, upstream["gold"], lines)
    assert not result.clean
    assert result.patch_id_match == [upstream["c"]]
    assert result.line_hits == {FIX_LINE.strip(): [upstream["c"]]}


def test_patch_id_ignores_whitespace_and_line_numbers(upstream):
    assert patch_id(upstream["gold"]) == patch_id(upstream["gold"].replace("@@ -1,2", "@@ -9,2"))
    assert patch_id("") is None


def test_history_overlap_metrics_and_recoverable_threshold():
    from repogym.env.history import history_overlap

    hits = {f"line {i}": ["c1"] for i in range(5)} | {"line 5": ["c2", "c1"], "line 6": ["c2"]}
    o = history_overlap(hits, distinctive=10)
    assert (o.top_commit, o.top_commit_lines, o.lines, o.max_fraction) == ("c1", 6, 7, 0.6)
    assert o.recoverable  # one past commit holds >= 50% and >= 5 distinctive lines
    small = history_overlap({"a": ["c"], "b": ["c"]}, distinctive=2)
    assert small.max_fraction == 1.0 and not small.recoverable  # 100%, but only 2 lines
    spread = history_overlap({f"l{i}": [f"c{i}"] for i in range(8)}, distinctive=10)
    assert spread.max_fraction == 0.1 and not spread.recoverable
    none = history_overlap({}, distinctive=4)
    assert none.top_commit is None and none.max_fraction == 0.0 and not none.recoverable


def test_past_overlap_alone_does_not_fail_the_probe(upstream, tmp_path):
    # The base's own ancestors re-add a line the fix also adds: context, not a leak (D6).
    bundle = make_bundle(upstream["bare"], upstream["b"], tmp_path / "t.bundle")
    gold = upstream["gold"] + "+    return value\n"  # 'return value' was added by commit A
    result = probe_bundle(bundle, gold, ["return value"])
    assert result.line_hits == {"return value": [upstream["a"]]}
    assert result.clean and not result.overlap().recoverable
