import subprocess

import pytest

from repogym.config import RepoSpec
from repogym.spec import (
    Defined,
    Reference,
    added_line_numbers,
    added_text,
    check_spec,
    collect_defined,
    find_spec_issues,
    referenced_names,
)
from repogym.tasktype import classify_task_type, type_from_labels


def test_added_line_numbers_follow_the_new_file():
    patch = "@@ -1,3 +1,4 @@\n a\n-b\n+B\n+C\n c\n@@ -10,1 +11,2 @@\n x\n+y\n"
    assert added_line_numbers(patch) == {2, 3, 12}


def test_collect_defined_finds_defs_params_assignments_and_exports():
    source = (
        "from .core import exported\n"
        "X = 1\n"
        "class C:\n"
        "    def method(self, a, *, kw=None):\n"
        "        self.attr = a\n"
        "flag = option('--from-source')\n"
    )
    defined = collect_defined({"pkg/m.py": source})
    assert {"exported", "X", "C", "method", "kw", "attr"} <= defined.names
    assert "--from-source" in defined.strings


def test_local_only_ignores_imports_and_call_keywords():
    defined = collect_defined(
        {"t.py": "from pkg import new\nf(new_kw=1)\ndef helper(): pass\n"}, True
    )
    assert "helper" in defined.names
    assert "new" not in defined.names and "new_kw" not in defined.names


TEST_FILE = """\
import pkg
from pkg import old_func, brand_new
from other import unrelated

@pkg.option("--local-opt")
def cmd(local_opt):
    pass

def test_it():
    pkg.old_func(color=True, shiny=1)
    obj.added_method()
    run(["--new-flag"])
    run(["--local-opt"])
"""
ADDED = set(range(1, 14))


def _refs():
    return referenced_names("tests/test_a.py", TEST_FILE, ADDED, ("pkg",))


def test_references_cover_imports_attributes_keywords_and_options():
    found = {(r.kind, r.name) for r in _refs()}
    assert ("import", "brand_new") in found
    assert ("attribute", "added_method") in found
    assert ("keyword", "shiny") in found
    assert ("cli_option", "--new-flag") in found
    assert ("import", "unrelated") not in found  # not from the package


def test_options_defined_by_the_test_itself_are_not_references():
    names = {r.name for r in _refs()}
    assert "--local-opt" in names  # passed on the command line...
    # ...but excluded later, because the test defines it (local_opt parameter).


def test_only_added_lines_count():
    refs = referenced_names("tests/test_a.py", TEST_FILE, {11}, ("pkg",))
    assert [r.name for r in refs] == ["added_method"]


BASE = collect_defined({"pkg/core.py": "def old_func(color=False): pass\nclass Obj: pass\n"})
GOLD = "def brand_new(): ...\ndef added_method(self): ...\nshiny = 1\nadd('--new-flag')\n"


def _issues(issue_text="", gold=GOLD):
    tests_defined = collect_defined({"tests/test_a.py": TEST_FILE}, local_only=True)
    return find_spec_issues(_refs(), BASE, tests_defined, gold, issue_text)


def test_new_names_the_issue_never_mentions_are_flagged():
    flagged = " ".join(_issues())
    for name in ("brand_new", "added_method", "shiny", "--new-flag"):
        assert f"`{name}`" in flagged
    for name in ("old_func", "color", "--local-opt", "pkg"):
        assert f"`{name}`" not in flagged


def test_names_mentioned_in_the_issue_are_fine():
    issue = "Please add brand_new() and an added_method; `shiny` too. Also new_flag."
    assert _issues(issue) == []


def test_option_mentioned_with_dashes_is_fine():
    assert not any("--new-flag" in i for i in _issues("support --new-flag please"))


def test_names_not_introduced_by_the_gold_patch_are_not_flagged():
    """E.g. pytest or mock attributes: new to the test, but the fix doesn't add them."""
    assert _issues(gold="") == []


def test_private_names_are_ignored():
    refs = [Reference("attribute", "_secret", "t.py", 1)]
    assert find_spec_issues(refs, Defined(), Defined(), "_secret", "") == []


def test_word_boundaries_in_mentions():
    refs = [Reference("attribute", "color", "t.py", 1)]
    assert find_spec_issues(refs, Defined(), Defined(), "color = 1", "colorful output") != []


def test_added_text_skips_headers():
    assert added_text("+++ b/x.py\n+new\n-old\n context\n") == "new"


@pytest.fixture
def tiny_repo(tmp_path, monkeypatch):
    """A local bare repo: pkg/core.py at the base commit, no network."""
    work = tmp_path / "work"
    work.mkdir()

    def git(*args):
        subprocess.run(["git", *args], cwd=work, check=True, capture_output=True)

    git("init", "-q")
    (work / "pkg").mkdir()
    (work / "pkg" / "core.py").write_text("def old(): return 1\n")
    (work / "tests").mkdir()
    (work / "tests" / "test_core.py").write_text("from pkg.core import old\n")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    bare = tmp_path / "pkg.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)], check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, check=True, capture_output=True, text=True
    ).stdout.strip()
    spec = RepoSpec("me/pkg", ("pkg/",))
    monkeypatch.setattr(RepoSpec, "bare_path", property(lambda self: bare))
    return spec, sha


TEST_PATCH = """\
diff --git a/tests/test_core.py b/tests/test_core.py
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1 +1,4 @@
-from pkg.core import old
+from pkg.core import old, shiny_new
+
+def test_new():
+    assert shiny_new(strict=True) == old()
"""
GOLD_PATCH = """\
diff --git a/pkg/core.py b/pkg/core.py
--- a/pkg/core.py
+++ b/pkg/core.py
@@ -1 +1,2 @@
 def old(): return 1
+def shiny_new(strict=False): return 1
"""


def test_check_spec_end_to_end(tiny_repo):
    spec, sha = tiny_repo
    result = check_spec(spec, sha, GOLD_PATCH, TEST_PATCH, "old() is wrong", "It returns 1.")
    assert not result.spec_clear
    assert any("`shiny_new`" in issue for issue in result.spec_issues)
    assert any("`strict`" in issue for issue in result.spec_issues)

    named = check_spec(spec, sha, GOLD_PATCH, TEST_PATCH, "Add shiny_new(strict=...)", "")
    assert named.spec_clear and named.spec_issues == []


def test_task_type_from_labels():
    assert type_from_labels(["Bug 🪳"]) == "bug"
    assert type_from_labels(["Crash 💥", "astroid-brain"]) == "bug"
    assert type_from_labels(["Enhancement ✨"]) == "feature"
    assert type_from_labels(["Feature"]) == "feature"
    assert type_from_labels(["bug", "enhancement"]) == "unknown"
    assert type_from_labels(["help output"]) == "unknown"
    assert type_from_labels([]) == "unknown"


def test_classifier_hook_only_runs_when_labels_are_inconclusive():
    from tests.test_validate import _candidate

    calls = []

    def classifier(candidate):
        calls.append(candidate.task_id)
        return "feature"

    unlabelled = _candidate()
    assert classify_task_type(unlabelled, classifier) == "feature"
    labelled = unlabelled.model_copy(update={"pr_labels": ["bug"]})
    assert classify_task_type(labelled, classifier) == "bug"
    assert calls == [unlabelled.task_id]


def test_stdlib_attributes_and_keywords_are_not_new_api():
    refs = [
        Reference("attribute", "join", "t.py", 1),
        Reference("attribute", "TextIOWrapper", "t.py", 2),
        Reference("keyword", "parents", "t.py", 3, callee="mkdir"),
    ]
    gold = "x = ', '.join(parts)\nio.TextIOWrapper(b)\np.mkdir(parents=True)"
    assert find_spec_issues(refs, Defined(), Defined(), gold, "") == []


def test_keyword_to_a_library_function_sharing_a_stdlib_name_is_still_checked():
    refs = [Reference("keyword", "colour", "t.py", 1, callee="open")]
    base = Defined(names={"open"})  # the library defines its own open()
    assert find_spec_issues(refs, base, Defined(), "def open(colour=None): ...", "") != []


def test_check_spec_ignores_non_python_files_in_the_test_patch(tiny_repo):
    spec, sha = tiny_repo
    patch = TEST_PATCH + (
        "diff --git a/tests/data.yml b/tests/data.yml\n"
        "--- a/tests/data.yml\n+++ b/tests/data.yml\n@@ -1 +1 @@\n-a: 1\n+a: 2\n"
    )
    result = check_spec(spec, sha, GOLD_PATCH, patch, "old() is wrong", "")
    assert any("`shiny_new`" in issue for issue in result.spec_issues)


def test_pytest_and_debugger_names_are_not_new_api():
    refs = [Reference("attribute", "raises", "t.py", 1), Reference("attribute", "Pdb", "t.py", 2)]
    assert find_spec_issues(refs, Defined(), Defined(), "raises\nPdb", "") == []


DELETE_AND_ADD_PATCH = """\
diff --git a/tests/test_core.py b/tests/test_core.py
deleted file mode 100644
--- a/tests/test_core.py
+++ /dev/null
@@ -1 +0,0 @@
-from pkg.core import old
diff --git a/tests/test_new.py b/tests/test_new.py
new file mode 100644
--- /dev/null
+++ b/tests/test_new.py
@@ -0,0 +1,3 @@
+from pkg.core import old, shiny_new
+def test_new():
+    assert shiny_new() == old()
"""


def test_check_spec_handles_deleted_test_files(tiny_repo):
    """git apply needs the old file present to delete it; otherwise the whole check fails."""
    spec, sha = tiny_repo
    result = check_spec(spec, sha, GOLD_PATCH, DELETE_AND_ADD_PATCH, "old() is wrong", "")
    assert [issue.split(" (")[0] for issue in result.spec_issues] == ["import `shiny_new`"]
