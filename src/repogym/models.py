"""Pydantic schemas shared by the mining, environment and validation stages."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class Candidate(BaseModel):
    """A merged PR that passed the mining filters; not yet validated."""

    task_id: str
    repo: str
    pr_number: int
    issue_number: int
    issue_title: str
    issue_body: str
    issue_labels: list[str] = Field(default_factory=list)
    link_source: str
    base_commit: str
    gold_patch: str
    test_patch: str
    created_at: str
    changed_lines: int
    source_files: list[str]
    test_files: list[str]
    other_files: list[str] = Field(default_factory=list)


class VerifiedTask(BaseModel):
    """A candidate that passed every validity check."""

    task_id: str
    repo: str
    base_commit: str
    issue_title: str
    issue_body: str
    gold_patch: str
    test_patch: str
    fail_to_pass: list[str]
    pass_to_pass: list[str]
    docker_image: str


class TestStatus(StrEnum):
    __test__ = False  # not a pytest test class

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    XFAILED = "xfailed"


class RunStage(StrEnum):
    """How far a container run got. Anything but COMPLETED means no usable test results."""

    CHECKOUT_FAILED = "checkout_failed"
    SUBMODULE_FAILED = "submodule_failed"
    INSTALL_FAILED = "install_failed"
    PATCH_FAILED = "patch_failed"
    TEST_PATCH_FAILED = "test_patch_failed"
    NO_TEST_FILES = "no_test_files"
    NO_RESULTS = "no_results"
    TIMEOUT = "timeout"
    OOM = "oom"
    COMPLETED = "completed"


class TestRun(BaseModel):
    """Structured result of one container run."""

    __test__ = False  # not a pytest test class

    stage: RunStage
    tests: dict[str, TestStatus] = Field(default_factory=dict)
    collection_errors: list[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    detail: str = ""
    pytest_exit_code: int | None = None


class Rejection(BaseModel):
    task_id: str
    repo: str
    reason: str
    detail: str = ""


class BaselineRecord(BaseModel):
    """What fails at base_commit with no patches; these tests can never enter a task's lists."""

    task_id: str
    repo: str
    failing: list[str]
    collection_errors: list[str]
