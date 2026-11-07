"""Episode setup. Runs INSIDE the agent container, as root, before the agent's first action.

Standard library only: the images are the validated v0.1 images and stay unchanged. It reads one
JSON job from stdin and prints one JSON report. The job carries no task metadata: no task id,
gold patch, test patch, test names or split. Only the base commit, which is the repo's own HEAD,
and the target package's import names.

Steps: build /work from the bundle (base_commit's ancestors only), install it exactly as
validation does, strip refs/reflogs, create the unprivileged `agent` user, then check the history
and the installed copies. The host runs the remaining checks (network, mounts, environment and
metadata scans) through docker.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys

WORK = "/work"
AGENT = "agent"
AGENT_HOME = "/home/agent"
MARKER = "/tmp/.repogym-setup"
REF = "refs/heads/repogym"
SKIP_DIRS = ("/proc", "/sys", "/dev", WORK)


def sh(*args, cwd=WORK, check=True, input_text=None):
    done = subprocess.run(args, cwd=cwd, text=True, capture_output=True, input=input_text)
    if check and done.returncode != 0:
        fail(f"{' '.join(args[:4])} failed: {(done.stdout + done.stderr)[-1500:]}")
    return done


def git(*args, cwd=WORK, check=True):
    return sh("git", "-c", "core.hooksPath=/dev/null", *args, cwd=cwd, check=check)


def fail(message):
    print(json.dumps({"ok": False, "error": message}))
    sys.exit(0)


def build_repo(job):
    os.makedirs(WORK, exist_ok=True)
    git("init", "--quiet")
    git("fetch", "--quiet", "--no-tags", job["bundle"], f"{REF}:{REF}")
    git("symbolic-ref", "HEAD", REF)
    git("checkout", "--quiet", "--force", "repogym")
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != job["base_commit"]:
        fail(f"HEAD {head} is not base_commit {job['base_commit']}")
    for sub in job["submodules"]:
        # Submodule content as plain files with no git history of its own.
        target = os.path.join(WORK, sub["path"])
        scratch = "/tmp/repogym-sub"
        git("init", "--quiet", "--bare", scratch, cwd="/")
        git(
            "--git-dir",
            scratch,
            "fetch",
            "--quiet",
            "--no-tags",
            sub["bundle"],
            f"{REF}:{REF}",
            cwd="/",
        )
        os.makedirs(target, exist_ok=True)
        git(
            "--git-dir",
            scratch,
            "--work-tree",
            target,
            "checkout",
            "--quiet",
            "--force",
            sub["commit"],
            "--",
            ".",
            cwd="/",
        )
        shutil.rmtree(scratch)


def install():
    done = sh(
        sys.executable,
        "-m",
        "pip",
        "install",
        "--quiet",
        "--no-deps",
        "--no-build-isolation",
        "--no-index",
        "-e",
        ".",
        check=False,
    )
    if done.returncode != 0:
        fail(f"install failed: {(done.stdout + done.stderr)[-1500:]}")


def strip_history(job):
    for remote in git("remote").stdout.split():
        git("remote", "remove", remote)
    for tag in git("tag", "--list").stdout.split():
        git("tag", "-d", tag)
    for ref in git("for-each-ref", "--format=%(refname)").stdout.split():
        if ref != REF:
            git("update-ref", "-d", ref)
    git("reflog", "expire", "--expire=now", "--expire-unreachable=now", "--all")
    shutil.rmtree(os.path.join(WORK, ".git", "logs"), ignore_errors=True)
    for name in ("FETCH_HEAD", "ORIG_HEAD", "packed-refs"):
        path = os.path.join(WORK, ".git", name)
        if os.path.exists(path):
            os.remove(path)
    # packRefs=false: the one branch stays a loose ref, so packed-refs never exists.
    git("-c", "gc.packRefs=false", "gc", "--quiet", "--prune=now")
    for path in [job["bundle"]] + [sub["bundle"] for sub in job["submodules"]]:
        os.remove(path)


def create_agent():
    sh(
        "useradd",
        "--create-home",
        "--home-dir",
        AGENT_HOME,
        "--shell",
        "/bin/bash",
        "--uid",
        "1000",
        AGENT,
        cwd="/",
    )
    with open(os.path.join(AGENT_HOME, ".gitconfig"), "w") as handle:
        handle.write("[user]\n\tname = agent\n\temail = agent@repogym.invalid\n")
    sh("chown", "-R", f"{AGENT}:{AGENT}", WORK, AGENT_HOME, cwd="/")


def check_history(job):
    """Every history assertion. Returns a list of problems (empty = clean) and some counts."""
    problems = []
    base = job["base_commit"]
    remotes = git("remote").stdout.split()
    tags = git("tag", "--list").stdout.split()
    refs = git("for-each-ref", "--format=%(refname) %(objectname)").stdout.splitlines()
    if remotes:
        problems.append(f"remotes present: {remotes}")
    if tags:
        problems.append(f"tags present: {tags[:5]}")
    if refs != [f"{REF} {base}"]:
        problems.append(f"refs other than {REF}={base}: {refs[:5]}")
    if git("symbolic-ref", "HEAD").stdout.strip() != REF:
        problems.append("HEAD is not refs/heads/repogym")
    future = git("rev-list", "--all", "--not", base).stdout.split()
    if future:
        problems.append(f"{len(future)} commits reachable that are not ancestors of base")
    ancestors = set(git("rev-list", base).stdout.split())
    objects = git("cat-file", "--batch-all-objects", "--batch-check").stdout.splitlines()
    commits = {row.split()[0] for row in objects if row.split()[1:2] == ["commit"]}
    stray = commits - ancestors
    if stray:
        problems.append(f"{len(stray)} commit objects in the store are not ancestors of base")
    unreachable = git(
        "fsck", "--unreachable", "--no-reflogs", "--no-progress", check=False
    ).stdout.split("\n")
    unreachable = [row for row in unreachable if row.startswith("unreachable")]
    if unreachable:
        problems.append(f"{len(unreachable)} unreachable objects")
    reflog = git("reflog", "--all", check=False).stdout.strip()
    if reflog or os.path.exists(os.path.join(WORK, ".git", "logs")):
        problems.append("reflog is not empty")
    for name in ("FETCH_HEAD", "ORIG_HEAD", "packed-refs"):
        if os.path.exists(os.path.join(WORK, ".git", name)):
            problems.append(f".git/{name} present")
    digest = hashlib.sha256("\n".join(sorted(commits)).encode()).hexdigest()
    return problems, {"commits": len(commits), "commit_set_sha256": digest}


def _walk_outside_work():
    for top in os.listdir("/"):
        start = "/" + top
        if start in SKIP_DIRS or os.path.islink(start):
            continue
        for root, dirs, files in os.walk(start):
            dirs[:] = [d for d in dirs if os.path.join(root, d) not in SKIP_DIRS]
            yield root, dirs, files


PROBE = r"""
import importlib, importlib.metadata as m, json, sys
names = json.loads(sys.argv[1])
out = {"files": {}, "editable": [], "others": []}
for n in names:
    out["files"][n] = getattr(importlib.import_module(n), "__file__", None)
for dist in m.distributions():
    name = dist.metadata["Name"]
    url = json.loads(dist.read_text("direct_url.json") or "{}")
    editable = url.get("dir_info", {}).get("editable") and url.get("url") == "file:///work"
    if editable:
        out["editable"].append(name)
        continue
    top = (dist.read_text("top_level.txt") or "").split()
    record = [r.split(",")[0] for r in (dist.read_text("RECORD") or "").splitlines()]
    provides = any(n in top or any(p == n + ".py" or p.startswith(n + "/") for p in record)
                   for n in names)
    if provides:
        out["others"].append(f"{name} {dist.version}")
print(json.dumps(out))
"""


def scan_copies(job):
    """No other copy of the package's source anywhere: site-packages, wheels, sdists, caches.

    The import and distribution checks run separately, as the agent user (see PROBE)."""
    wanted = set(job["packages"])
    prefixes = tuple(n.lower().replace("_", "-") + "-" for n in job["packages"] + job["dists"])
    copies = []
    for root, dirs, files in _walk_outside_work():
        for d in dirs:
            if d in wanted and os.path.exists(os.path.join(root, d, "__init__.py")):
                copies.append(os.path.join(root, d))
        # Every target package is a directory, so a real copy (installed, vendored or cached)
        # is a directory of that name with an __init__.py, or an archive of the distribution.
        # Same-named single files, such as hypothesis's internal attrs.py, are other code.
        for f in files:
            low = f.lower().replace("_", "-")
            if low.startswith(prefixes) and low.endswith((".whl", ".tar.gz", ".zip")):
                copies.append(os.path.join(root, f))
    return [f"other copies of the target package outside /work: {copies[:10]}"] if copies else []


def main():
    job = json.load(sys.stdin)
    build_repo(job)
    install()
    strip_history(job)
    create_agent()
    history_problems, history = check_history(job)
    copy_problems = scan_copies(job)
    untracked = git("ls-files", "--others", "--exclude-standard", "-z").stdout
    ignored = git(
        "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"
    ).stdout
    with open(MARKER, "w") as handle:
        handle.write("setup done\n")
    print(
        json.dumps(
            {
                "ok": not history_problems and not copy_problems,
                "history": history,
                "history_problems": history_problems,
                "copy_problems": copy_problems,
                # Files that exist after setup but aren't tracked (build artifacts of the install);
                # excluded when the agent's diff is extracted.
                "setup_artifacts": sorted(p for p in (untracked + ignored).split("\0") if p),
            }
        )
    )


if __name__ == "__main__":
    main()
