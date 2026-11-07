"""The upstream licence of each task, read from its repository at the task's base commit.

Each task's code, patches and issue text come from its upstream repository and stay under that
licence. The SPDX identifier is taken from the repo's own ``pyproject.toml`` at the base commit
and cross-checked against the licence file's text. A licence that can't be identified, or whose
declaration contradicts the text, is an error: it is never guessed.
"""

from __future__ import annotations

import hashlib
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

LICENSE_FILES = ("LICENSE", "LICENSE.txt", "LICENSE.md", "LICENSE.rst", "COPYING", "LICENCE")

# Canonical SPDX spelling for the identifiers our repos use (SPDX ids are case-insensitive).
CANONICAL = {
    "mit": "MIT",
    "bsd-3-clause": "BSD-3-Clause",
    "lgpl-2.1-or-later": "LGPL-2.1-or-later",
    "lgpl-2.1-only": "LGPL-2.1-only",
    "apache-2.0": "Apache-2.0",
}


class LicenseError(ValueError):
    pass


@dataclass(frozen=True)
class UpstreamLicense:
    spdx: str
    file: str
    file_sha256: str
    source: str  # "pyproject" or "license text"


def text_family(text: str) -> str | None:
    """Which licence a licence file's text is, from distinctive wording; None if unknown."""
    flat = " ".join(text.split()).lower()
    if "permission is hereby granted, free of charge" in flat:
        return "MIT"
    if "redistribution and use in source and binary forms" in flat:
        clauses = len(re.findall(r"(?:^|\s)[1-4]\.\s", text))
        if "neither the name" in flat and clauses == 3:
            return "BSD-3-Clause"
        return None
    if "gnu lesser general public license" in flat and "version 2.1" in flat:
        return "LGPL-2.1"
    if "apache license" in flat and "version 2.0" in flat:
        return "Apache-2.0"
    return None


def declared_spdx(pyproject: str) -> str | None:
    """SPDX id from ``license = "X"`` or ``license = {text = "X"}``; None if absent or a file."""
    match = re.search(
        r'^\s*license\s*=\s*(?:"([^"]+)"|\{\s*text\s*=\s*"([^"]+)"\s*\})', pyproject, re.M
    )
    if not match:
        return None
    value = (match.group(1) or match.group(2)).strip()
    canonical = CANONICAL.get(value.lower())
    if canonical is None:
        raise LicenseError(f"unrecognised licence declaration {value!r}")
    return canonical


def _consistent(spdx: str, family: str) -> bool:
    return spdx == family or (family == "LGPL-2.1" and spdx.startswith("LGPL-2.1-"))


def identify(files: dict[str, bytes], pyproject: str | None) -> UpstreamLicense:
    """Licence from the licence file(s) and pyproject at one commit."""
    present = [name for name in LICENSE_FILES if name in files]
    if len(present) != 1:
        raise LicenseError(f"expected one licence file, found {present or 'none'}")
    name = present[0]
    text = files[name].decode("utf-8", errors="replace")
    family = text_family(text)
    if family is None:
        raise LicenseError(f"{name}: licence text not recognised")
    spdx = declared_spdx(pyproject or "")
    if spdx is None:
        if family == "LGPL-2.1":  # "only" vs "or later" is not in the text itself
            raise LicenseError(f"{name}: LGPL-2.1 text without a declared version scope")
        spdx, source = family, "license text"
    else:
        source = "pyproject"
    if not _consistent(spdx, family):
        raise LicenseError(f"declared {spdx} but {name} reads as {family}")
    return UpstreamLicense(spdx, name, hashlib.sha256(files[name]).hexdigest(), source)


def _show(bare: Path, commit: str, path: str) -> bytes | None:
    done = subprocess.run(["git", "-C", str(bare), "show", f"{commit}:{path}"], capture_output=True)
    return done.stdout if done.returncode == 0 else None


def license_at(bare: Path, commit: str) -> UpstreamLicense:
    """Read and identify the licence in a bare clone at ``commit``."""
    names = subprocess.run(
        ["git", "-C", str(bare), "ls-tree", "--name-only", commit],
        capture_output=True, text=True, check=True,
    ).stdout.split()  # fmt: skip
    files = {n: data for n in names if n in LICENSE_FILES and (data := _show(bare, commit, n))}
    pyproject = _show(bare, commit, "pyproject.toml")
    return identify(files, pyproject.decode("utf-8", errors="replace") if pyproject else None)
