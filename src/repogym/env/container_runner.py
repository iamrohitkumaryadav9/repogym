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
TAIL = 2000


def sh(*args, input_text=None):
    return subprocess.run(args, cwd=WORK, input=input_text, text=True, capture_output=True)


def finish(stage, detail="", **extra):
    print(json.dumps({"stage": stage, "detail": detail[-TAIL:], **extra}))
    sys.exit(0)


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

    start = time.monotonic()
    done = sh(
        sys.executable, "-m", "pytest", "--quiet", "--tb=short", "-p", "no:cacheprovider",
        "--continue-on-collection-errors", f"--junitxml={JUNIT}", *files,
    )  # fmt: skip
    duration = time.monotonic() - start
    output = done.stdout + done.stderr
    if not os.path.exists(JUNIT):
        stage = "oom" if done.returncode in (-9, 137) else "no_results"
        finish(stage, f"pytest exit {done.returncode}\n{output}")
    with open(JUNIT, encoding="utf-8", errors="replace") as handle:
        xml = handle.read()
    finish("completed", output, junit_xml=xml, exit_code=done.returncode, duration=duration)


main()
