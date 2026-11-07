"""Paths, limits and per-repository settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
REPOS_DIR = DATA_DIR / "repos"
CANDIDATES_PATH = DATA_DIR / "tasks_candidates.jsonl"
MINING_REPORT_PATH = DATA_DIR / "mining_report.jsonl"
VERIFIED_PATH = DATA_DIR / "tasks_verified.jsonl"
REJECTIONS_PATH = DATA_DIR / "rejections.jsonl"
BASELINE_PATH = DATA_DIR / "baseline_failures.jsonl"
RUN_METRICS_PATH = DATA_DIR / "run_metrics.jsonl"
PROVENANCE_PATH = DATA_DIR / "provenance.jsonl"
TASK_TYPE_LABELS_PATH = DATA_DIR / "task_type_labels.jsonl"
TASK_TYPE_RUNS_PATH = DATA_DIR / "task_type_runs.jsonl"
LABELLING_DIR = DATA_DIR / "labelling"
TASK_TYPE_SHEET = LABELLING_DIR / "task_type_sheet.csv"
SPEC_SHEET = LABELLING_DIR / "spec_sheet.csv"
RELEASE_DIR = DATA_DIR / "release"
BUILD_LOG_PATH = DATA_DIR / "image_builds.jsonl"

MINING_WINDOW_DAYS = 3 * 365
MAX_CHANGED_LINES = 300

DEFAULT_WORKERS = 2
CONTAINER_MEMORY = "2g"
CONTAINER_MEMORY_BYTES = 2 * 1024**3
# An OOM kill without the fix counts as a failing test only if the fixed code stays far below
# the limit; otherwise the "bug" might just be a test that is heavy on a small container.
OOM_ORACLE_PEAK_FRACTION = 0.5
CONTAINER_CPUS = "2"
RUN_TIMEOUT_SECONDS = 300  # whole container; a backstop behind the per-test timeout
PER_TEST_TIMEOUT_SECONDS = 30
# A listed test must pass well inside the per-test timeout in every oracle run, so that a
# slow-but-correct fix on a busier machine is not mistaken for a hang.
ORACLE_TEST_SECONDS_LIMIT = PER_TEST_TIMEOUT_SECONDS / 3
DOCKER_BUDGET_BYTES = 10 * 1000**3

PYTHON_IMAGE = "python:3.11.11-slim-bookworm"
IMAGE_LABEL = "repogym=1"
BUILDER_NAME = "repogym-builder"


@dataclass(frozen=True)
class RepoSpec:
    """How to classify and run one repository."""

    full_name: str
    source_roots: tuple[str, ...]
    # Submodule URLs as they appear in .gitmodules -> name of the local bare mirror.
    submodule_mirrors: dict[str, str] = field(default_factory=dict)
    # Extra Debian packages the test suite needs at run time.
    apt_packages: tuple[str, ...] = ()

    @property
    def slug(self) -> str:
        return self.full_name.split("/")[1]

    @property
    def packages(self) -> tuple[str, ...]:
        """Top-level import names provided by the source roots."""
        return tuple(dict.fromkeys(root.rstrip("/").split("/")[-1] for root in self.source_roots))

    @property
    def image(self) -> str:
        return f"repogym/{self.slug}:base"

    @property
    def bare_path(self) -> Path:
        return REPOS_DIR / f"{self.slug}.git"

    @property
    def submodule_remotes(self) -> dict[str, str]:
        """Bare mirror name -> upstream URL to clone it from."""
        remotes: dict[str, str] = {}
        for url, mirror in self.submodule_mirrors.items():
            if url.startswith("https://"):
                remotes.setdefault(mirror, url)
        return remotes


_TOML_TEST = "toml-test"
_SPEC_TESTS = "toml-spec-tests"

REPOS: dict[str, RepoSpec] = {
    spec.slug: spec
    for spec in (
        RepoSpec(
            "pallets/click",
            ("src/click/",),
            # Pager tests spawn `less`; without it one test closes the captured stdout and
            # every later test in tests/test_utils.py errors during setup.
            apt_packages=("less",),
        ),
        RepoSpec("python-attrs/attrs", ("src/attr/", "src/attrs/")),
        RepoSpec("PyCQA/isort", ("isort/",)),
        RepoSpec(
            "python-poetry/tomlkit",
            ("tomlkit/",),
            submodule_mirrors={
                "https://github.com/BurntSushi/toml-test.git": _TOML_TEST,
                "git@github.com:BurntSushi/toml-test.git": _TOML_TEST,
                "https://github.com/toml-lang/toml-test.git": _TOML_TEST,
                "https://github.com/iarna/toml-spec-tests.git": _SPEC_TESTS,
                "git@github.com:iarna/toml-spec-tests.git": _SPEC_TESTS,
            },
        ),
        RepoSpec("pylint-dev/astroid", ("astroid/",)),
    )
}

# Held back; only enabled if fewer than 50 tasks verify from the repos above.
RESERVE_REPOS: dict[str, RepoSpec] = {
    "marshmallow": RepoSpec("marshmallow-code/marshmallow", ("src/marshmallow/",)),
}


def get_repo(slug_or_name: str) -> RepoSpec:
    slug = slug_or_name.split("/")[-1]
    if slug in REPOS:
        return REPOS[slug]
    if slug in RESERVE_REPOS:
        return RESERVE_REPOS[slug]
    raise KeyError(f"Unknown repo: {slug_or_name}")
