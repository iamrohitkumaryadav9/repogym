"""Runs inside a task container. Standard library only; reads a job as JSON from stdin.

Steps: check out base_commit from the read-only bare clone, install it, apply the patches, run
the given test files, and print one JSON object with the raw JUnit XML to stdout.
"""

import json
import os
import subprocess
import sys
import time

WORK = "/work"
JUNIT = "/tmp/junit.xml"
PROGRESS = "/tmp/repogym_progress.jsonl"
PLUGIN_DIR = "/tmp/repogym_plugin"
TAIL = 2000
PROGRESS_LIMIT = 20000

# Logs every test as it starts and finishes, line-buffered, so the results of tests that ran
# before an OOM kill survive even though pytest never gets to write its JUnit file.
PLUGIN = """
import json
_log = open("__PROGRESS__", "a", buffering=1)

def pytest_runtest_logstart(nodeid, location):
    _log.write(json.dumps({"nodeid": nodeid, "event": "start"}) + "\\n")

def pytest_runtest_logreport(report):
    _log.write(json.dumps({
        "nodeid": report.nodeid, "event": "report", "when": report.when,
        "outcome": report.outcome, "xfail": hasattr(report, "wasxfail"),
    }) + "\\n")
""".replace("__PROGRESS__", PROGRESS)


def sh(*args, input_text=None):
    return subprocess.run(args, cwd=WORK, input=input_text, text=True, capture_output=True)


def peak_memory_bytes():
    """The container cgroup's peak memory use (cgroup v2, falling back to v1)."""
    for path in ("/sys/fs/cgroup/memory.peak", "/sys/fs/cgroup/memory/memory.max_usage_in_bytes"):
        try:
            with open(path) as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            continue
    return None


def pytest_version():
    try:
        from importlib.metadata import version

        return version("pytest")
    except Exception:  # noqa: BLE001 - provenance only
        return None


def finish(stage, detail="", **extra):
    payload = {
        "stage": stage,
        "detail": detail[-TAIL:],
        "peak_memory_bytes": peak_memory_bytes(),
        "pytest_version": pytest_version(),
        **extra,
    }
    print(json.dumps(payload))
    sys.exit(0)


def read_progress():
    try:
        with open(PROGRESS, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return []
    return lines[-PROGRESS_LIMIT:]


def apply_patch(patch, failed_stage):
    if not patch:
        return
    done = sh("git", "apply", "--index", "--whitespace=nowarn", "-", input_text=patch)
    if done.returncode != 0:
        finish(failed_stage, done.stderr)


def update_submodules():
    if not os.path.exists(os.path.join(WORK, ".gitmodules")):
        return
    done = sh("git", "submodule", "update", "--init", "--recursive", "--quiet", "--force")
    if done.returncode != 0:
        finish("submodule_failed", done.stderr)


def main():
    job = json.load(sys.stdin)
    os.makedirs(WORK, exist_ok=True)
    sh("git", "init", "--quiet")
    with open(os.path.join(WORK, ".git/objects/info/alternates"), "w") as handle:
        handle.write(job["bare_repo"] + "/objects\n")
    for url, mirror in job["submodule_mirrors"].items():
        sh("git", "config", "--global", "--add", f"url.file://{mirror}.insteadOf", url)

    done = sh("git", "checkout", "--quiet", "--force", job["base_commit"])
    if done.returncode != 0:
        finish("checkout_failed", done.stderr)
    update_submodules()

    done = sh(
        sys.executable, "-m", "pip", "install", "--quiet", "--no-deps", "--no-build-isolation",
        "--no-index", "-e", ".",
    )  # fmt: skip
    if done.returncode != 0:
        finish("install_failed", done.stdout + done.stderr)

    apply_patch(job["patch"], "patch_failed")
    apply_patch(job["test_patch"], "test_patch_failed")
    if "Subproject commit" in job["test_patch"]:
        update_submodules()

    files = [path for path in job["test_files"] if os.path.isfile(os.path.join(WORK, path))]
    if not files:
        if job["require_test_files"]:
            finish("no_test_files", "None of the touched test files exist after patching")
        finish("completed", junit_xml="", exit_code=None, duration=0.0)

    os.makedirs(PLUGIN_DIR, exist_ok=True)
    with open(os.path.join(PLUGIN_DIR, "repogym_progress.py"), "w") as handle:
        handle.write(PLUGIN)
    env = dict(os.environ, PYTHONPATH=PLUGIN_DIR)
    start = time.monotonic()
    done = subprocess.run(
        [
        sys.executable, "-m", "pytest", "--quiet", "--tb=short", "-p", "no:cacheprovider",
        "--continue-on-collection-errors", f"--junitxml={JUNIT}",
        # A hanging test fails on its own instead of taking the whole run down with it.
        f"--timeout={job['per_test_timeout']}", "--timeout-method=signal",
        "-p", "repogym_progress",
        *files,
        ],
        cwd=WORK, text=True, capture_output=True, env=env,
    )  # fmt: skip
    duration = time.monotonic() - start
    output = done.stdout + done.stderr
    if not os.path.exists(JUNIT):
        stage = "oom" if done.returncode in (-9, 137) else "no_results"
        finish(
            stage,
            f"pytest exit {done.returncode}\n{output}",
            progress=read_progress(),
            duration=duration,
        )
    with open(JUNIT, encoding="utf-8", errors="replace") as handle:
        xml = handle.read()
    finish("completed", output, junit_xml=xml, exit_code=done.returncode, duration=duration)


main()
