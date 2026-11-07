"""Flag tasks whose tests depend on names the issue never mentions.

If the fix adds a public name (a function, class, method, keyword parameter or CLI option), the
tests use that name, and the issue never mentions it, an agent can only guess the interface. Such a
task measures luck rather than skill, so it is flagged, but kept, with ``spec_clear=False``.

A name is flagged only if all of these hold:

* the test lines added by ``test_patch`` reference it, as an import from the package, an attribute,
  a keyword argument or a ``--cli-option`` string;
* it is public (no leading underscore);
* nothing in the package source at ``base_commit`` defines it (and, for an option, no string
  in the source contains it);
* the touched test files don't define it themselves (helpers, fixtures, test-local commands);
* the gold patch's added lines contain it, so the fix introduces it;
* the issue title and body don't mention it.
"""

from __future__ import annotations

import ast
import functools
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from repogym.config import RepoSpec
from repogym.mining.patches import is_runnable_test_file, split_diff

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")
_DIFF_PATHS = re.compile(r'^diff --git "?a/(?P<a>.*?)"? "?b/(?P<b>.*?)"?$', re.MULTILINE)
_OPTION = re.compile(r"^--[a-z0-9][a-z0-9-]*$")
_OPTION_DEFINERS = ("option", "argument", "add_argument", "Option", "Argument")


@functools.cache
def stdlib_names() -> frozenset[str]:
    """Attribute names of builtins and stdlib modules that tests commonly touch.

    A test calling ``", ".join(...)`` or ``tmp_path.mkdir(parents=True)`` is not using a new
    library API, even if the fix happens to contain the same word.
    """
    import builtins
    import collections
    import contextlib
    import copy
    import dataclasses
    import datetime
    import enum
    import functools as functools_module
    import inspect
    import io
    import itertools
    import json
    import logging
    import operator
    import os
    import pathlib
    import pdb
    import shutil
    import signal
    import string
    import subprocess
    import sys
    import tempfile
    import textwrap
    import threading
    import time
    import traceback
    import types
    import typing
    import unittest.mock
    import warnings

    objects: list[object] = [
        builtins, str, bytes, list, dict, set, frozenset, tuple, int, float, object, type,
        BaseException, collections, contextlib, dataclasses, enum, functools_module, inspect, io,
        io.StringIO, io.BytesIO, io.TextIOWrapper, itertools, json, os, os.path, pathlib,
        pathlib.Path, re, re.compile(""), re.match("", ""), shutil, subprocess,
        subprocess.CompletedProcess, sys, tempfile, textwrap, typing, unittest.mock,
        unittest.mock.MagicMock(), warnings, copy, datetime, datetime.datetime, logging, operator,
        pdb, signal, string, threading, time, traceback, types,
    ]  # fmt: skip
    try:
        import pytest

        objects += [pytest, pytest.MonkeyPatch, pytest.CaptureFixture]
    except ImportError:  # pragma: no cover - pytest is a dev dependency
        pass
    names: set[str] = set()
    for obj in objects:
        names.update(dir(obj))
    return frozenset(names)


def added_line_numbers(file_patch: str) -> set[int]:
    """Line numbers, in the patched file, of the lines this file's diff adds."""
    added: set[int] = set()
    line_no = 0
    in_hunk = False
    for line in file_patch.split("\n"):
        match = _HUNK.match(line)
        if match:
            line_no = int(match["start"])
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            added.add(line_no)
            line_no += 1
        elif line.startswith(" "):
            line_no += 1
        elif line.startswith("\\") or line.startswith("-"):
            continue
        else:
            in_hunk = False
    return added


@dataclass
class Defined:
    names: set[str] = field(default_factory=set)
    strings: list[str] = field(default_factory=list)


def collect_defined(sources: dict[str, str], local_only: bool = False) -> Defined:
    """Names that code defines or exports: defs, classes, parameters, assignments and imports.

    With ``local_only`` (used for test files), imports and call keywords don't count: a test
    importing or passing a new name is exactly what we are looking for, not a definition of it.
    """
    defined = Defined()
    for text in sources.values():
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                defined.names.add(node.name)
            elif isinstance(node, ast.arg):
                defined.names.add(node.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.names.add(node.id)
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                defined.names.add(node.attr)
            elif local_only:
                continue
            elif isinstance(node, ast.alias):
                defined.names.add((node.asname or node.name).split(".")[0])
                defined.names.add(node.name.split(".")[-1])
            elif isinstance(node, ast.keyword) and node.arg:
                # attrs/dataclass-style fields, e.g. attrib(alias="x"), define names via keywords.
                defined.names.add(node.arg)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                defined.strings.append(node.value)
    return defined


@dataclass(frozen=True)
class Reference:
    kind: str
    name: str
    path: str
    line: int
    callee: str = ""  # for keywords: the called function's name

    def describe(self) -> str:
        return f"{self.kind} `{self.name}` ({self.path}:{self.line})"


def referenced_names(
    path: str, text: str, added: set[int], packages: tuple[str, ...]
) -> list[Reference]:
    """Library names that the added lines of one test file use."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    option_definitions: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _OPTION_DEFINERS:
                option_definitions.update(id(arg) for arg in node.args)

    refs: list[Reference] = []
    for node in ast.walk(tree):
        line = getattr(node, "lineno", None)
        if line not in added:
            continue
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module.split(".")[0] in packages:
                refs += [Reference("import", a.name, path, line) for a in node.names]
        elif isinstance(node, ast.Attribute):
            refs.append(Reference("attribute", node.attr, path, line))
        elif isinstance(node, ast.Call):
            func = node.func
            callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            refs += [
                Reference("keyword", k.arg, path, line, callee) for k in node.keywords if k.arg
            ]
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _OPTION.match(node.value) and id(node) not in option_definitions:
                refs.append(Reference("cli_option", node.value, path, line))
    return refs


def _mentioned(word: str, text: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text) is not None


def _option_variants(option: str) -> list[str]:
    bare = option.lstrip("-")
    return [option, bare, bare.replace("-", "_")]


def find_spec_issues(
    references: list[Reference],
    base_source: Defined,
    tests_defined: Defined,
    gold_added_text: str,
    issue_text: str,
) -> list[str]:
    issues: dict[str, str] = {}
    for ref in references:
        if ref.name.startswith("_") or ref.name in issues:
            continue
        if ref.kind == "cli_option":
            variants = _option_variants(ref.name)
            if any(ref.name in s for s in base_source.strings):
                continue
            if any(v in tests_defined.names for v in variants[1:]):
                continue
            if not any(_mentioned(v, gold_added_text) for v in variants):
                continue
            if any(_mentioned(v, issue_text) for v in variants):
                continue
        else:
            if ref.name in base_source.names or ref.name in tests_defined.names:
                continue
            if ref.kind == "attribute" and ref.name in stdlib_names():
                continue
            if (
                ref.kind == "keyword"
                and ref.callee in stdlib_names()
                and ref.callee not in base_source.names
            ):
                continue
            if not _mentioned(ref.name, gold_added_text):
                continue
            if _mentioned(ref.name, issue_text):
                continue
        issues[ref.name] = ref.describe()
    return list(issues.values())


def added_text(patch: str) -> str:
    return "\n".join(
        line[1:]
        for line in patch.split("\n")
        if line.startswith("+") and not line.startswith("+++")
    )


# --- git access (host side) -------------------------------------------------------------------


def _git(spec: RepoSpec, *args: str, input_text: str | None = None) -> str:
    done = subprocess.run(
        ["git", "--git-dir", str(spec.bare_path), *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=True,
    )
    return done.stdout


def read_files(spec: RepoSpec, commit: str, paths: list[str]) -> dict[str, str]:
    """Contents of ``paths`` at ``commit``; missing paths are left out."""
    files: dict[str, str] = {}
    for path in paths:
        done = subprocess.run(
            ["git", "--git-dir", str(spec.bare_path), "show", f"{commit}:{path}"],
            capture_output=True,
        )
        if done.returncode == 0:
            files[path] = done.stdout.decode("utf-8", errors="replace")
    return files


def source_files_at(spec: RepoSpec, commit: str) -> dict[str, str]:
    listing = _git(spec, "ls-tree", "-r", "--name-only", commit, "--", *spec.source_roots)
    paths = [p for p in listing.splitlines() if p.endswith((".py", ".pyi"))]
    return read_files(spec, commit, paths)


def patched_test_files(spec: RepoSpec, commit: str, test_patch: str) -> dict[str, str]:
    """Touched test files after ``test_patch``, applied with plain ``git apply`` in a temp dir."""
    paths = [f.path for f in split_diff(test_patch) if f.path.endswith(".py") and not f.is_deleted]
    # Deleted files and rename sources must exist too, or git apply refuses the whole patch.
    old_paths = [m["a"] for m in _DIFF_PATHS.finditer(test_patch) if m["a"].endswith(".py")]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for path, text in read_files(spec, commit, sorted({*paths, *old_paths})).items():
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        subprocess.run(
            # Only the Python files were materialised; skip data and yaml files in the patch.
            ["git", "apply", "--whitespace=nowarn", "--include=*.py", "-"],
            cwd=root,
            input=test_patch,
            text=True,
            capture_output=True,
            check=True,
        )
        return {p: (root / p).read_text(encoding="utf-8") for p in paths if (root / p).exists()}


@dataclass(frozen=True)
class SpecResult:
    spec_clear: bool
    spec_issues: list[str]


def check_spec(
    spec: RepoSpec,
    base_commit: str,
    gold_patch: str,
    test_patch: str,
    issue_title: str,
    issue_body: str,
) -> SpecResult:
    after = patched_test_files(spec, base_commit, test_patch)
    added = {f.path: added_line_numbers(f.text) for f in split_diff(test_patch)}
    references: list[Reference] = []
    for path, text in after.items():
        if is_runnable_test_file(path):
            references += referenced_names(path, text, added.get(path, set()), spec.packages)
    issues = find_spec_issues(
        references,
        base_source=collect_defined(source_files_at(spec, base_commit)),
        tests_defined=collect_defined(after, local_only=True),
        gold_added_text=added_text(gold_patch),
        issue_text=f"{issue_title}\n{issue_body}",
    )
    return SpecResult(spec_clear=not issues, spec_issues=issues)
