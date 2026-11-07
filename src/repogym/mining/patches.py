"""Split a unified PR diff into source and test patches."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath

_DIFF_HEADER = re.compile(r'^diff --git "?a/(?P<a>.*?)"? "?b/(?P<b>.*?)"?$')
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(?P<old>\d+))? \+\d+(?:,(?P<new>\d+))? @@")
_TEST_DIRS = {"test", "tests"}


class FileKind(StrEnum):
    SOURCE = "source"
    TEST = "test"
    OTHER = "other"


@dataclass(frozen=True)
class FileDiff:
    path: str
    text: str
    changed_lines: int
    is_binary: bool = False
    is_deleted: bool = False


def classify_path(path: str, source_roots: tuple[str, ...]) -> FileKind:
    """Classify a repo path. Test directories win; then source roots; then test filenames.

    Source roots outrank filename patterns because packages can ship modules such as
    ``astroid/test_utils.py`` that are library code despite their name.
    """
    pure = PurePosixPath(path)
    if any(part in _TEST_DIRS for part in pure.parts[:-1]):
        return FileKind.TEST
    if any(path.startswith(root) for root in source_roots):
        return FileKind.SOURCE
    name = pure.name
    if name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py"):
        return FileKind.TEST
    return FileKind.OTHER


def is_runnable_test_file(path: str) -> bool:
    """True for files pytest should be pointed at (not fixtures, data or conftest)."""
    pure = PurePosixPath(path)
    return (
        pure.suffix == ".py"
        and pure.name != "conftest.py"
        and (pure.name.startswith(("test_", "unittest_")) or pure.name.endswith("_test.py"))
    )


def count_changed_lines(file_diff: str) -> int:
    """Count added and removed lines using hunk sizes.

    Hunk-aware counting avoids mistaking content such as a removed ``-- x`` line (which
    renders as ``--- x``) for a file header.
    """
    changed = 0
    old_left = new_left = 0
    for line in file_diff.split("\n"):
        if old_left > 0 or new_left > 0:
            if line.startswith("+"):
                changed += 1
                new_left -= 1
            elif line.startswith("-"):
                changed += 1
                old_left -= 1
            elif line.startswith("\\"):
                pass  # "\ No newline at end of file"
            else:
                old_left -= 1
                new_left -= 1
            continue
        match = _HUNK_HEADER.match(line)
        if match:
            old_left = int(match["old"]) if match["old"] is not None else 1
            new_left = int(match["new"]) if match["new"] is not None else 1
    return changed


def split_diff(diff: str) -> list[FileDiff]:
    """Split a multi-file git diff into per-file diffs, preserving text byte-for-byte."""
    chunks: list[list[str]] = []
    in_hunk = False
    old_left = new_left = 0
    for line in diff.split("\n"):
        if old_left <= 0 and new_left <= 0:
            in_hunk = False
        if in_hunk:
            if line.startswith("+"):
                new_left -= 1
            elif line.startswith("-"):
                old_left -= 1
            elif not line.startswith("\\"):
                old_left -= 1
                new_left -= 1
            chunks[-1].append(line)
            continue
        if line.startswith("diff --git "):
            chunks.append([line])
            continue
        if not chunks:
            continue
        match = _HUNK_HEADER.match(line)
        if match:
            old_left = int(match["old"]) if match["old"] is not None else 1
            new_left = int(match["new"]) if match["new"] is not None else 1
            in_hunk = True
        chunks[-1].append(line)

    files: list[FileDiff] = []
    for lines in chunks:
        header = _DIFF_HEADER.match(lines[0].rstrip("\r"))
        if header is None:
            raise ValueError(f"Unparseable diff header: {lines[0]!r}")
        # A trailing "" comes from the final newline; rejoin so each chunk ends in exactly one.
        while lines and lines[-1] == "":
            lines.pop()
        text = "\n".join(lines) + "\n"
        head = text.split("@@", 1)[0]
        files.append(
            FileDiff(
                path=header["b"],
                text=text,
                changed_lines=count_changed_lines(text),
                is_binary="Binary files " in head or "GIT binary patch" in head,
                is_deleted="deleted file mode" in head,
            )
        )
    return files


@dataclass(frozen=True)
class SplitPatch:
    gold_patch: str
    test_patch: str
    source_files: list[str]
    test_files: list[str]
    other_files: list[str]
    changed_lines: int
    has_binary: bool


def split_patch(diff: str, source_roots: tuple[str, ...]) -> SplitPatch:
    """Partition a PR diff. Changes to docs, changelogs and config go in neither patch.

    ``changed_lines`` counts source and test lines only, so a regenerated lock file or a long
    changelog entry can't push an otherwise small fix over the size limit.
    """
    gold: list[str] = []
    test: list[str] = []
    source_files: list[str] = []
    test_files: list[str] = []
    other_files: list[str] = []
    changed = 0
    has_binary = False
    for file in split_diff(diff):
        kind = classify_path(file.path, source_roots)
        if kind is FileKind.OTHER:
            other_files.append(file.path)
            continue
        has_binary = has_binary or file.is_binary
        changed += file.changed_lines
        if kind is FileKind.SOURCE:
            gold.append(file.text)
            source_files.append(file.path)
        else:
            test.append(file.text)
            test_files.append(file.path)
    return SplitPatch(
        gold_patch="".join(gold),
        test_patch="".join(test),
        source_files=source_files,
        test_files=test_files,
        other_files=other_files,
        changed_lines=changed,
        has_binary=has_binary,
    )
