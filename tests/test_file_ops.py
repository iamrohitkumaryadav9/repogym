"""File tools: exact edits, informative errors, and no escape from /work (symlinks included)."""

import os

import pytest

from repogym.tools import file_ops
from repogym.tools.file_ops import ToolError, create_file, resolve, str_replace, view_file


@pytest.fixture
def work(tmp_path, monkeypatch):
    root = tmp_path / "work"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("outside\n")
    monkeypatch.setattr(file_ops, "WORK", str(root))
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("a = 1\nb = 2\nb = 2\n")
    return root


def test_paths_resolve_inside_work(work):
    assert resolve("pkg/mod.py") == str(work / "pkg" / "mod.py")
    assert resolve(str(work / "pkg" / "new.py")) == str(work / "pkg" / "new.py")
    assert resolve("pkg/missing/dir/new.py").startswith(str(work))


@pytest.mark.parametrize("path", ["../secret.txt", "/etc/passwd", "pkg/../../secret.txt", ""])
def test_paths_outside_work_are_refused(work, path):
    with pytest.raises(ToolError):
        resolve(path)


def test_symlink_to_outside_is_refused_for_every_tool(work, tmp_path):
    os.symlink(tmp_path / "secret.txt", work / "link.txt")
    os.symlink(tmp_path, work / "escape")  # a symlinked directory
    for request in (
        {"path": "link.txt"},
        {"path": "escape/secret.txt"},
        {"path": "escape/new_dir/new.py", "content": "x"},
    ):
        with pytest.raises(ToolError, match="outside /work"):
            resolve(request["path"])
    with pytest.raises(ToolError, match="outside"):
        create_file({"path": "escape/planted.py", "content": "x"})
    assert not (tmp_path / "planted.py").exists()
    with pytest.raises(ToolError, match="outside"):
        str_replace({"path": "link.txt", "old": "outside", "new": "pwned"})
    assert (tmp_path / "secret.txt").read_text() == "outside\n"


def test_view_numbers_lines_and_reports_ranges(work):
    out = view_file({"path": "pkg/mod.py", "start_line": 2, "end_line": 3})
    assert out.splitlines() == ["pkg/mod.py (lines 2-3 of 3)", "     2\tb = 2", "     3\tb = 2"]
    with pytest.raises(ToolError, match="has 3 lines"):
        view_file({"path": "pkg/mod.py", "start_line": 9})
    with pytest.raises(ToolError, match="is a directory"):
        view_file({"path": "pkg"})
    with pytest.raises(ToolError, match="no such file"):
        view_file({"path": "nope.py"})


def test_view_limits_range_and_refuses_binary(work):
    (work / "big.txt").write_text("x\n" * 1000)
    with pytest.raises(ToolError, match="at most 400"):
        view_file({"path": "big.txt", "start_line": 1, "end_line": 900})
    assert "(lines 1-400 of 1000)" in view_file({"path": "big.txt"})
    (work / "blob.bin").write_bytes(b"\x00\x01")
    with pytest.raises(ToolError, match="binary"):
        view_file({"path": "blob.bin"})


def test_str_replace_needs_one_exact_match(work):
    with pytest.raises(ToolError, match="matches 2 times.*lines 2, 3"):
        str_replace({"path": "pkg/mod.py", "old": "b = 2", "new": "b = 3"})
    with pytest.raises(ToolError, match="not found"):
        str_replace({"path": "pkg/mod.py", "old": "c = 3", "new": "c = 4"})
    with pytest.raises(ToolError, match="identical"):
        str_replace({"path": "pkg/mod.py", "old": "a = 1", "new": "a = 1"})
    with pytest.raises(ToolError, match="must not be empty"):
        str_replace({"path": "pkg/mod.py", "old": "", "new": "x"})
    assert "at line 1" in str_replace({"path": "pkg/mod.py", "old": "a = 1", "new": "a = 9"})
    assert (work / "pkg" / "mod.py").read_text().startswith("a = 9\n")


def test_create_file_makes_parents_and_reports(work):
    assert create_file({"path": "pkg/sub/new.py", "content": "x = 1\n"}) == (
        "created pkg/sub/new.py (1 lines)"
    )
    assert create_file({"path": "pkg/sub/new.py", "content": "y\nz\n"}).startswith("overwrote")
    with pytest.raises(ToolError, match="is a directory"):
        create_file({"path": "pkg", "content": "x"})
