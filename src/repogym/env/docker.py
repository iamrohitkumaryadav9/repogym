"""Docker operations: image builds, task runs, disk usage and cleanup."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import structlog

from repogym.config import (
    CONTAINER_CPUS,
    CONTAINER_MEMORY,
    IMAGE_LABEL,
    PYTHON_IMAGE,
    REPOS_DIR,
    RUN_TIMEOUT_SECONDS,
    RepoSpec,
)
from repogym.env.results import parse_junit
from repogym.models import RunStage, TestRun

log = structlog.get_logger()

_RUNNER = Path(__file__).with_name("container_runner.py")
_CONTAINER_REPOS = "/repos"
_DOCKER_CLI_ERROR = 125
_OOM_EXIT = 137
_DETAIL_LIMIT = 2000


class DockerUnavailable(RuntimeError):
    """The daemon itself failed. Says nothing about the task, so nothing may be recorded for it."""


def _docker(*args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], input=input_text, text=True, capture_output=True)


def check_docker() -> None:
    done = _docker("version", "--format", "{{.Server.Version}}")
    if done.returncode != 0:
        raise DockerUnavailable(done.stderr.strip() or "docker is not reachable")


def build_image(spec: RepoSpec) -> None:
    package = resources.files("repogym.env")
    with tempfile.TemporaryDirectory() as context:
        ctx = Path(context)
        (ctx / "Dockerfile").write_text((package / "Dockerfile").read_text())
        requirements = package / "requirements" / f"{spec.slug}.txt"
        (ctx / "requirements.txt").write_text(requirements.read_text())
        done = subprocess.run(
            [
                "docker",
                "build",
                "--quiet",
                "--build-arg",
                f"PYTHON_IMAGE={PYTHON_IMAGE}",
                "--label",
                IMAGE_LABEL,
                "-t",
                spec.image,
                str(ctx),
            ],  # fmt: skip
            text=True,
            capture_output=True,
        )
    if done.returncode != 0:
        raise RuntimeError(f"Building {spec.image} failed:\n{done.stderr[-3000:]}")


def run_tests(
    spec: RepoSpec,
    base_commit: str,
    patch: str,
    test_patch: str,
    test_files: list[str],
    require_test_files: bool = True,
    timeout: int = RUN_TIMEOUT_SECONDS,
) -> TestRun:
    """Run one job in a fresh, network-less, memory-limited container, always removed after."""
    job = {
        "bare_repo": f"{_CONTAINER_REPOS}/{spec.bare_path.name}",
        "base_commit": base_commit,
        "patch": patch,
        "test_patch": test_patch,
        "test_files": test_files,
        "require_test_files": require_test_files,
        "submodule_mirrors": {
            url: f"{_CONTAINER_REPOS}/{mirror}.git"
            for url, mirror in spec.submodule_mirrors.items()
        },
    }
    name = f"repogym-{uuid.uuid4().hex[:12]}"
    command = [
        "docker", "run", "--rm", "-i",
        "--name", name,
        "--label", IMAGE_LABEL,
        "--network", "none",
        "--memory", CONTAINER_MEMORY,
        "--memory-swap", CONTAINER_MEMORY,
        "--cpus", CONTAINER_CPUS,
        "--pids-limit", "1024",
        "-v", f"{REPOS_DIR}:{_CONTAINER_REPOS}:ro",
        "-v", f"{_RUNNER}:/runner.py:ro",
        spec.image, "python", "/runner.py",
    ]  # fmt: skip
    try:
        done = subprocess.run(
            command, input=json.dumps(job), text=True, capture_output=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return TestRun(stage=RunStage.TIMEOUT, detail=f"Exceeded {timeout}s")
    finally:
        # --rm handles normal exits; a timeout kills only the CLI, not the container.
        _docker("rm", "--force", name)

    return _parse_run(done.returncode, done.stdout, done.stderr)


def _parse_run(returncode: int, stdout: str, stderr: str) -> TestRun:
    if returncode == _OOM_EXIT:
        return TestRun(stage=RunStage.OOM, detail="Container killed (exit 137)")
    if returncode == _DOCKER_CLI_ERROR or _is_daemon_error(stderr):
        raise DockerUnavailable(stderr.strip()[-500:])
    try:
        payload = json.loads(stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        return TestRun(
            stage=RunStage.NO_RESULTS,
            detail=f"exit {returncode}: {(stdout + stderr)[-_DETAIL_LIMIT:]}",
        )
    stage = RunStage(payload["stage"])
    if stage is not RunStage.COMPLETED:
        return TestRun(stage=stage, detail=payload.get("detail", ""))
    parsed = parse_junit(payload.get("junit_xml", ""))
    return TestRun(
        stage=RunStage.COMPLETED,
        tests=parsed.tests,
        collection_errors=parsed.collection_errors,
        duration_seconds=payload.get("duration", 0.0),
        detail=parsed.first_error,
        pytest_exit_code=payload.get("exit_code"),
    )


def _is_daemon_error(stderr: str) -> bool:
    return "Cannot connect to the Docker daemon" in stderr or "error during connect" in stderr


_SIZE = re.compile(r"^\s*([\d.]+)\s*([kKMGT]?)B")
_UNITS = {"": 1, "k": 1000, "K": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4}


def parse_size(text: str) -> int:
    match = _SIZE.match(text)
    return int(float(match[1]) * _UNITS[match[2]]) if match else 0


@dataclass(frozen=True)
class DiskUsage:
    total_bytes: int
    repogym_bytes: int
    by_type: dict[str, int]


def disk_usage() -> DiskUsage:
    done = _docker("system", "df", "--format", "{{json .}}")
    if done.returncode != 0:
        raise DockerUnavailable(done.stderr.strip())
    by_type: dict[str, int] = {}
    for line in done.stdout.splitlines():
        row = json.loads(line)
        by_type[row["Type"]] = parse_size(row["Size"])
    images = _docker(
        "image", "ls", "--filter", f"label={IMAGE_LABEL}", "--format", "{{.ID}} {{.Size}}"
    )
    # Layer sharing makes this an upper bound on what repogym actually occupies.
    sizes = dict(line.split(" ", 1) for line in images.stdout.splitlines() if line.strip())
    return DiskUsage(sum(by_type.values()), sum(parse_size(s) for s in sizes.values()), by_type)


def clean(keep_images: set[str], other_projects: bool = False) -> dict[str, int]:
    """Remove repogym's stopped containers, dangling images and images no task needs.

    Docker resources belonging to other projects are left alone unless ``other_projects``.
    """
    removed = {"containers": 0, "dangling_images": 0, "unused_images": 0}
    filters = ["--filter", "status=exited", "--filter", "status=created"]
    if not other_projects:
        filters += ["--filter", f"label={IMAGE_LABEL}"]
    stopped = _docker("ps", "-aq", *filters).stdout.split()
    if stopped:
        _docker("rm", *stopped)
        removed["containers"] = len(stopped)

    filters = ["--filter", "dangling=true"]
    if not other_projects:
        filters += ["--filter", f"label={IMAGE_LABEL}"]
    dangling = _docker("image", "ls", "-q", *filters).stdout.split()
    if dangling:
        _docker("image", "rm", *dangling)
        removed["dangling_images"] = len(dangling)

    listing = _docker(
        "image", "ls", "--filter", f"label={IMAGE_LABEL}", "--format", "{{.Repository}}:{{.Tag}}"
    )
    for image in listing.stdout.split():
        if image.endswith(":<none>") or image in keep_images:
            continue
        if _docker("image", "rm", image).returncode == 0:
            removed["unused_images"] += 1
    return removed
