from repogym.mining.patches import (
    FileKind,
    classify_path,
    count_changed_lines,
    is_runnable_test_file,
    split_diff,
    split_patch,
)

ROOTS = ("src/pkg/",)

DIFF = """\
diff --git a/CHANGES.md b/CHANGES.md
index 111..222 100644
--- a/CHANGES.md
+++ b/CHANGES.md
@@ -1,1 +1,2 @@
 Version 1
+- Fixed the bug.
diff --git a/src/pkg/core.py b/src/pkg/core.py
index 111..222 100644
--- a/src/pkg/core.py
+++ b/src/pkg/core.py
@@ -1,2 +1,2 @@
 def f():
-    return 1
+    return 2
diff --git a/tests/test_core.py b/tests/test_core.py
index 111..222 100644
--- a/tests/test_core.py
+++ b/tests/test_core.py
@@ -1,1 +1,3 @@
 import pkg
+def test_f():
+    assert pkg.f() == 2
"""


def test_split_routes_each_file_to_the_right_patch():
    split = split_patch(DIFF, ROOTS)
    assert split.source_files == ["src/pkg/core.py"]
    assert split.test_files == ["tests/test_core.py"]
    assert split.other_files == ["CHANGES.md"]
    assert "return 2" in split.gold_patch and "test_f" not in split.gold_patch
    assert "test_f" in split.test_patch and "return 2" not in split.test_patch
    assert "Fixed the bug" not in split.gold_patch + split.test_patch


def test_changed_lines_excludes_files_in_neither_patch():
    assert split_patch(DIFF, ROOTS).changed_lines == 4


def test_patches_end_with_exactly_one_newline():
    split = split_patch(DIFF, ROOTS)
    assert split.gold_patch.endswith("return 2\n")
    assert split.test_patch.endswith("== 2\n")


def test_removed_line_that_looks_like_a_file_header_is_counted():
    diff = "@@ -1,2 +1,1 @@\n--- not a header\n keep\n"
    assert count_changed_lines("diff --git a/x b/x\n" + diff) == 1


def test_added_line_that_looks_like_a_diff_header_does_not_split_the_file():
    diff = (
        "diff --git a/tests/test_x.py b/tests/test_x.py\n"
        "--- a/tests/test_x.py\n+++ b/tests/test_x.py\n"
        "@@ -1,0 +1,2 @@\n"
        "+diff --git a/fake b/fake\n"
        "+x = 1\n"
    )
    files = split_diff(diff)
    assert [f.path for f in files] == ["tests/test_x.py"]
    assert files[0].changed_lines == 2


def test_crlf_content_is_preserved():
    diff = (
        "diff --git a/src/pkg/a.py b/src/pkg/a.py\n"
        "--- a/src/pkg/a.py\n+++ b/src/pkg/a.py\n"
        "@@ -1,1 +1,1 @@\n-old\r\n+new\r\n"
    )
    assert "+new\r\n" in split_patch(diff, ROOTS).gold_patch


def test_no_newline_marker_is_not_counted():
    diff = (
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n"
        "\\ No newline at end of file\n+b\n\\ No newline at end of file\n"
    )
    assert split_diff(diff)[0].changed_lines == 2


def test_rename_uses_the_new_path():
    diff = (
        "diff --git a/src/pkg/old.py b/src/pkg/new.py\n"
        "similarity index 100%\nrename from src/pkg/old.py\nrename to src/pkg/new.py\n"
    )
    assert split_diff(diff)[0].path == "src/pkg/new.py"


def test_binary_and_deleted_files_are_flagged():
    diff = (
        "diff --git a/tests/data.bin b/tests/data.bin\n"
        "index 1..2 100644\nBinary files a/tests/data.bin and b/tests/data.bin differ\n"
        "diff --git a/tests/test_a.py b/tests/test_a.py\n"
        "deleted file mode 100644\n--- a/tests/test_a.py\n+++ /dev/null\n"
        "@@ -1,1 +0,0 @@\n-x = 1\n"
    )
    binary, deleted = split_diff(diff)
    assert binary.is_binary and not binary.is_deleted
    assert deleted.is_deleted and not deleted.is_binary
    assert split_patch(diff, ROOTS).has_binary


def test_classify_prefers_source_root_over_test_like_filename():
    assert classify_path("astroid/test_utils.py", ("astroid/",)) is FileKind.SOURCE


def test_classify_test_directory_beats_source_root():
    assert classify_path("src/pkg/tests/test_a.py", ROOTS) is FileKind.TEST


def test_classify_test_data_and_other_files():
    assert classify_path("tests/testdata/mod.py", ROOTS) is FileKind.TEST
    assert classify_path("docs/conf.py", ROOTS) is FileKind.OTHER
    assert classify_path("pyproject.toml", ROOTS) is FileKind.OTHER


def test_runnable_test_files_exclude_fixtures():
    assert is_runnable_test_file("tests/test_a.py")
    assert is_runnable_test_file("tests/a_test.py")
    assert is_runnable_test_file("tests/unittest_a.py")
    assert not is_runnable_test_file("tests/conftest.py")
    assert not is_runnable_test_file("tests/testdata/mod.py")
    assert not is_runnable_test_file("tests/data/test_input.toml")
