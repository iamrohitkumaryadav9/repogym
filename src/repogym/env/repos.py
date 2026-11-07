"""Bare clones on the host; containers mount them read-only."""

from __future__ import annotations

import subprocess
from pathlib import Path

import structlog

from repogym.config import REPOS_DIR, RepoSpec

log = structlog.get_logger()


def _git(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True)


def _ensure_bare(url: str, path: Path) -> None:
    if path.exists():
        done = _git("fetch", "--quiet", "origin", "+refs/heads/*:refs/heads/*", cwd=path)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        log.info("cloning", url=url)
        done = _git("clone", "--quiet", "--bare", url, str(path))
    if done.returncode != 0:
        raise RuntimeError(f"git failed for {url}: {done.stderr.strip()}")


def ensure_clones(spec: RepoSpec) -> None:
    """Create or update the repo's bare clone and any submodule mirrors it needs."""
    _ensure_bare(f"https://github.com/{spec.full_name}.git", spec.bare_path)
    for mirror, url in spec.submodule_remotes.items():
        _ensure_bare(url, REPOS_DIR / f"{mirror}.git")


def ensure_commit(spec: RepoSpec, sha: str) -> bool:
    """Make sure a commit is present, fetching it directly if no branch still reaches it."""
    if _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=spec.bare_path).returncode == 0:
        return True
    _git("fetch", "--quiet", "origin", sha, cwd=spec.bare_path)
    return _git("cat-file", "-e", f"{sha}^{{commit}}", cwd=spec.bare_path).returncode == 0
