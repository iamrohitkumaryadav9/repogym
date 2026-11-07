"""The agent's container: created, set up and checked before the agent's first action.

Host side. Everything here is enforced before an episode may start; any failed check raises
EnvSetupError and no episode runs. After setup, every command runs as the unprivileged ``agent``
user (never root), with a timeout, and its output is captured with a hard size cap.
"""

from __future__ import annotations

import io
import json
import re
import secrets
import subprocess
import tarfile
import tempfile
import threading
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from repogym.config import CONTAINER_MEMORY, RepoSpec
from repogym.env import sandbox_setup
from repogym.env.history import HistoryError, bundle_commits, commit_set_digest, git, make_bundle

AGENT = sandbox_setup.AGENT
AGENT_HOME = sandbox_setup.AGENT_HOME
WORK = sandbox_setup.WORK
EPISODE_LABELS = {"repogym": "1", "repogym.role": "episode"}
# Docker bind-mounts these three (generated) files into every container, even with
# --network none. They carry the random hostname and no task data; nothing else may be mounted.
DOCKER_MANAGED_MOUNTS = {"/etc/hostname", "/etc/hosts", "/etc/resolv.conf"}
ALLOWED_MOUNT_POINTS = {
    "/",
    "/proc",
    "/dev",
    "/sys",
    "/dev/pts",
    "/dev/mqueue",
    "/dev/shm",
    "/sys/fs/cgroup",
    "/dev/console",
} | DOCKER_MANAGED_MOUNTS
SETUP_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER")  # chown /work to the agent during setup
TASK_DATA_NAMES = re.compile(r"\.(patch|diff|bundle|rej|orig)$|tasks?.*\.jsonl$")
SPLIT_WORDS = re.compile(r"\b(train|dev|test)\b", re.IGNORECASE)


class EnvSetupError(RuntimeError):
    """An episode setup or leakage check failed. The episode must not start."""


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class SetupReport:
    container: str
    checks: list[Check] = field(default_factory=list)
    seconds: float = 0.0
    history: dict = field(default_factory=dict)
    setup_artifacts: list[str] = field(default_factory=list)
    baseline_changes: set[str] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]


@dataclass(frozen=True)
class Needles:
    """Task metadata that must never be visible in the container (host-side only)."""

    strings: tuple[str, ...]  # task id, PR/issue URLs, test node ids, distinctive patch lines
    numbers: tuple[str, ...]  # PR and issue numbers, matched as whole tokens

    def find(self, text: str) -> list[str]:
        hits = [s for s in self.strings if s and s in text]
        hits += [n for n in self.numbers if re.search(rf"(?<![\w.]){re.escape(n)}(?![\w.])", text)]
        return hits


@dataclass
class ExecResult:
    exit_code: int | None
    stdout: str
    stderr: str
    timed_out: bool
    seconds: float
    stdout_bytes: int
    stderr_bytes: int


class _Capped:
    """Keeps the first and last ``cap`` bytes of a stream, and counts everything."""

    def __init__(self, cap: int) -> None:
        self.cap, self.head, self.tail, self.total = cap, bytearray(), bytearray(), 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.cap - len(self.head)
        if room > 0:
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail = (self.tail + chunk)[-self.cap :]

    def text(self) -> str:
        """Head, then (if anything was dropped) a marker, then the tail."""
        middle = ""
        if self.total > 2 * self.cap:
            middle = f"\n[... {self.total - 2 * self.cap} bytes not captured ...]\n"
        return (
            bytes(self.head).decode("utf-8", errors="replace")
            + middle
            + bytes(self.tail).decode("utf-8", errors="replace")
        )


def _docker(
    *args: str, input_bytes: bytes | None = None, timeout: float | None = 120, check: bool = True
) -> subprocess.CompletedProcess:
    done = subprocess.run(
        ["docker", *args], input=input_bytes, capture_output=True, timeout=timeout
    )
    if check and done.returncode != 0:
        raise EnvSetupError(f"docker {args[0]} failed: {done.stderr.decode()[-800:]}")
    return done


def project_names(bare: Path, commit: str) -> list[str]:
    """Distribution name from the repo's pyproject.toml at ``commit`` (for the copy scan)."""
    try:
        data = tomllib.loads(git("--git-dir", str(bare), "show", f"{commit}:pyproject.toml"))
    except (HistoryError, tomllib.TOMLDecodeError):
        return []
    name = data.get("project", {}).get("name")
    return [name] if name else []


def submodule_pins(bare: Path, commit: str) -> list[tuple[str, str]]:
    """(path, pinned commit) for every gitlink in the tree at ``commit``."""
    rows = git("--git-dir", str(bare), "ls-tree", "-r", commit).splitlines()
    return [(row.split("\t", 1)[1], row.split()[2]) for row in rows if row.split()[1] == "commit"]


class Sandbox:
    def __init__(
        self,
        spec: RepoSpec,
        base_commit: str,
        needles: Needles,
        memory: str = CONTAINER_MEMORY,
        cpus: str = "2",
    ) -> None:
        self.spec = spec
        self.base_commit = base_commit
        self.needles = needles
        self.memory = memory
        self.cpus = cpus
        self.name = f"repogym-ep-{secrets.token_hex(6)}"
        self.hostname = secrets.token_hex(6)
        self.started = False

    # -- lifecycle ---------------------------------------------------------------------------

    def setup(self) -> SetupReport:
        """Create, build and check the container. Raises EnvSetupError if any check fails."""
        start = time.monotonic()
        report = SetupReport(container=self.name)
        try:
            self._create()
            job, digest = self._copy_in_bundles()
            _docker("start", self.name)
            self.started = True
            done = _docker(
                "exec",
                "-u",
                "0",
                "-i",
                self.name,
                "python3",
                "-c",
                _SETUP_SOURCE,
                input_bytes=json.dumps(job).encode(),
                timeout=900,
            )
            result = json.loads(done.stdout.decode().strip().splitlines()[-1])
            if "error" in result:
                raise EnvSetupError(f"setup failed: {result['error']}")
            report.history = result["history"]
            report.setup_artifacts = result["setup_artifacts"]
            report.checks.append(
                Check(
                    "history", not result["history_problems"], "; ".join(result["history_problems"])
                )
            )
            report.checks.append(
                Check(
                    "history_matches_bundle",
                    result["history"]["commit_set_sha256"] == digest,
                    f"container {result['history']['commits']} commits",
                )
            )
            report.checks.append(
                Check(
                    "no_other_copies",
                    not result["copy_problems"],
                    "; ".join(result["copy_problems"]),
                )
            )
            report.checks += self._check_installed()
            report.checks += self._check_network()
            report.checks += self._check_isolation()
            report.checks += self._check_metadata(report)
            # Probes above ran after the setup marker; reset it so only the agent's own
            # changes are newer, and take the baseline of changed paths from here.
            _docker(
                "exec",
                "-u",
                "0",
                self.name,
                "sh",
                "-c",
                f"rm -rf /tmp/repogym-pip-probe && touch {sandbox_setup.MARKER}",
            )
            report.baseline_changes = self.changed_paths()
        except EnvSetupError:
            self.remove()
            raise
        report.seconds = round(time.monotonic() - start, 1)
        if not report.ok:
            self.remove()
            failed = "; ".join(f"{c.name}: {c.detail}" for c in report.failed())
            raise EnvSetupError(f"leakage checks failed: {failed}")
        return report

    def remove(self) -> None:
        _docker("rm", "--force", self.name, check=False, timeout=60)

    def _create(self) -> None:
        caps = [arg for cap in SETUP_CAPS for arg in ("--cap-add", cap)]
        labels = [arg for k, v in EPISODE_LABELS.items() for arg in ("--label", f"{k}={v}")]
        _docker(
            "create",
            "--name",
            self.name,
            "--hostname",
            self.hostname,
            "--network",
            "none",
            "--memory",
            self.memory,
            "--memory-swap",
            self.memory,
            "--cpus",
            self.cpus,
            "--pids-limit",
            "512",
            "--cap-drop",
            "ALL",
            *caps,
            "--security-opt",
            "no-new-privileges",
            *labels,
            self.spec.image,
            "sleep",
            "infinity",
        )

    def _copy_in_bundles(self) -> tuple[dict, str]:
        with tempfile.TemporaryDirectory(prefix="repogym-ep-") as tmp:
            bundle = make_bundle(self.spec.bare_path, self.base_commit, Path(tmp) / "repo.bundle")
            digest = commit_set_digest(bundle_commits(bundle))
            _docker("cp", str(bundle), f"{self.name}:/tmp/repo.bundle")
            submodules = []
            for i, (path, pin) in enumerate(submodule_pins(self.spec.bare_path, self.base_commit)):
                mirror = _mirror_for(self.spec, path, self.base_commit)
                sub = make_bundle(mirror, pin, Path(tmp) / f"sub{i}.bundle")
                _docker("cp", str(sub), f"{self.name}:/tmp/sub{i}.bundle")
                submodules.append({"path": path, "commit": pin, "bundle": f"/tmp/sub{i}.bundle"})
        job = {
            "bundle": "/tmp/repo.bundle",
            "base_commit": self.base_commit,
            "submodules": submodules,
            "packages": list(self.spec.packages),
            "dists": project_names(self.spec.bare_path, self.base_commit),
        }
        return job, digest

    # -- checks run from the host ------------------------------------------------------------

    def _check_installed(self) -> list[Check]:
        done = self.exec_agent(
            ["python3", "-c", sandbox_setup.PROBE, json.dumps(list(self.spec.packages))],
            timeout=120,
        )
        if done.exit_code != 0:
            return [Check("installed_once", False, f"probe failed: {done.stderr[-500:]}")]
        found = json.loads(done.stdout)
        problems = [
            f"import {n} -> {p}"
            for n, p in found["files"].items()
            if not p or not p.startswith(WORK + "/")
        ]
        if len(found["editable"]) != 1:
            problems.append(f"editable installs of /work: {found['editable']}")
        if found["others"]:
            problems.append(f"other distributions provide it: {found['others']}")
        return [
            Check(
                "installed_once",
                not problems,
                "; ".join(problems) or f"{found['editable']} editable at /work",
            )
        ]

    def _check_network(self) -> list[Check]:
        net = _docker("inspect", "--format", "{{.HostConfig.NetworkMode}}", self.name)
        checks = [
            Check(
                "network_mode_none",
                net.stdout.decode().strip() == "none",
                net.stdout.decode().strip(),
            )
        ]
        done = self.exec_agent(["python3", "-c", _NETWORK_PROBE], timeout=90)
        try:
            result = json.loads(done.stdout)
        except json.JSONDecodeError:
            return checks + [Check("network_blocked", False, done.stderr[-500:])]
        open_paths = [name for name, blocked in result["blocked"].items() if not blocked]
        checks.append(
            Check(
                "network_blocked",
                not open_paths,
                f"reachable: {open_paths}" if open_paths else "dns, tcp, http, pip all fail",
            )
        )
        checks.append(
            Check(
                "interfaces_loopback_only",
                result["interfaces"] == ["lo"],
                str(result["interfaces"]),
            )
        )
        return checks

    def _check_isolation(self) -> list[Check]:
        info = json.loads(_docker("inspect", self.name).stdout)[0]
        checks = [
            Check("no_mounts", info["Mounts"] == [], str(info["Mounts"])),
            Check(
                "no_privileges",
                info["HostConfig"]["CapDrop"] == ["ALL"]
                and "no-new-privileges" in (info["HostConfig"]["SecurityOpt"] or []),
                f"caps added: {info['HostConfig']['CapAdd']}",
            ),
        ]
        mounts = self.exec_agent(["cat", "/proc/mounts"], timeout=30).stdout.splitlines()
        points = {line.split()[1] for line in mounts if len(line.split()) > 1}
        extra = sorted(
            p
            for p in points
            if p not in ALLOWED_MOUNT_POINTS and not p.startswith(("/proc/", "/sys/", "/dev/"))
        )
        checks.append(Check("no_host_paths_mounted", not extra, f"unexpected: {extra}"))
        user = self.exec_agent(["id", "-u"], timeout=30).stdout.strip()
        checks.append(Check("agent_is_unprivileged", user == "1000", f"uid {user}"))
        return checks

    def _check_metadata(self, report: SetupReport) -> list[Check]:
        """No task metadata in env vars, hostname, labels, or any file changed outside /work."""
        problems = []
        info = json.loads(_docker("inspect", self.name).stdout)[0]
        image = json.loads(_docker("image", "inspect", self.spec.image).stdout)[0]
        labels = info["Config"]["Labels"] or {}
        expected_labels = {**(image["Config"].get("Labels") or {}), **EPISODE_LABELS}
        # Docker Desktop stamps every container with desktop.docker.io/* labels; those are
        # allowed (and still scanned for task data below). Anything else is unexpected.
        extra_labels = {
            k: v
            for k, v in labels.items()
            if k not in expected_labels and not k.startswith("desktop.docker.io/")
        }
        if extra_labels or any(labels.get(k) != v for k, v in expected_labels.items()):
            problems.append(f"labels {labels}")
        env = self.exec_agent(["env", "-0"], timeout=30).stdout.split("\0")
        image_env = set(image["Config"].get("Env") or [])
        extra_env = [
            e
            for e in env
            if e and e not in image_env and e.split("=", 1)[0] not in ("HOSTNAME", "HOME")
        ]
        if extra_env:
            problems.append(f"unexpected env vars {extra_env}")
        visible = "\n".join([*env, self.hostname, json.dumps(labels)])
        for hit in self.needles.find(visible):
            problems.append(f"metadata in env/hostname/labels: {hit!r}")
        for word in SPLIT_WORDS.findall("\n".join([*extra_env, self.hostname, json.dumps(labels)])):
            problems.append(f"split name visible: {word!r}")
        changed = self.changed_paths()
        report.baseline_changes = changed
        outside = sorted(p for p in changed if not (p == WORK or p.startswith(WORK + "/")))
        for path in outside:
            if TASK_DATA_NAMES.search(path):
                problems.append(f"task-data-like file outside the repo: {path}")
        for path, data in self._read_files(outside).items():
            for hit in self.needles.find(data.decode("utf-8", errors="replace")):
                problems.append(f"metadata in {path}: {hit[:60]!r}")
        return [
            Check(
                "no_task_metadata",
                not problems,
                "; ".join(problems[:10]) or f"{len(outside)} changed paths outside /work scanned",
            )
        ]

    # -- helpers -----------------------------------------------------------------------------

    def changed_paths(self) -> set[str]:
        out = _docker("diff", self.name).stdout.decode().splitlines()
        return {line[2:] for line in out if len(line) > 2}

    def _read_files(self, paths: list[str]) -> dict[str, bytes]:
        """Regular files among ``paths``, read with a root tar during setup (before the agent)."""
        if not paths:
            return {}
        done = _docker(
            "exec",
            "-u",
            "0",
            "-i",
            self.name,
            "tar",
            "-cf",
            "-",
            "--no-recursion",
            "--ignore-failed-read",
            "-T",
            "-",
            input_bytes="\n".join(paths).encode(),
            check=False,
        )
        files = {}
        with tarfile.open(fileobj=io.BytesIO(done.stdout), mode="r:") as tar:
            for member in tar:
                if member.isfile() and member.size < 50_000_000:
                    handle = tar.extractfile(member)
                    if handle:
                        files["/" + member.name] = handle.read()
        return files

    def exec_agent(
        self,
        argv: list[str],
        timeout: float,
        stdin: bytes | None = None,
        cap_bytes: int = 1_000_000,
        workdir: str = WORK,
    ) -> ExecResult:
        """Run ``argv`` in the container as ``agent``, killed after ``timeout`` seconds."""
        command = ["docker", "exec", "-u", AGENT, "-w", workdir, "-e", f"HOME={AGENT_HOME}"]
        if stdin is not None:
            command.append("-i")
        command += [self.name, "timeout", "--kill-after=5", str(int(timeout)), *argv]
        start = time.monotonic()
        proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE if stdin is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, err = _Capped(cap_bytes), _Capped(cap_bytes)
        readers = [
            threading.Thread(target=_pump, args=(proc.stdout, out), daemon=True),
            threading.Thread(target=_pump, args=(proc.stderr, err), daemon=True),
        ]
        for reader in readers:
            reader.start()
        if stdin is not None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin)
                proc.stdin.close()
            except BrokenPipeError:
                pass
        timed_out = False
        try:
            proc.wait(timeout=timeout + 30)  # backstop; `timeout` inside normally fires first
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            timed_out = True
        for reader in readers:
            reader.join(timeout=10)
        code = proc.returncode
        if code in (124, 137) and time.monotonic() - start >= timeout - 1:
            timed_out = True
        return ExecResult(
            exit_code=None if timed_out else code,
            stdout=out.text(),
            stderr=err.text(),
            timed_out=timed_out,
            seconds=round(time.monotonic() - start, 2),
            stdout_bytes=out.total,
            stderr_bytes=err.total,
        )

    def copy_out(self, path: str) -> bytes:
        """A tar stream of ``path`` from the container (docker cp: no exec, no agent code)."""
        done = _docker("cp", f"{self.name}:{path}", "-", timeout=600)
        return done.stdout


def _pump(stream, sink: _Capped) -> None:
    for chunk in iter(lambda: stream.read(65536), b""):
        sink.feed(chunk)


def _mirror_for(spec: RepoSpec, path: str, commit: str) -> Path:
    """The local bare mirror for the submodule at ``path`` (from .gitmodules at ``commit``)."""
    from repogym.config import REPOS_DIR

    modules = git("--git-dir", str(spec.bare_path), "show", f"{commit}:.gitmodules")
    url = None
    current = None
    for line in modules.splitlines():
        line = line.strip()
        if line.startswith("path"):
            current = line.split("=", 1)[1].strip()
        elif line.startswith("url") and current == path:
            url = line.split("=", 1)[1].strip()
    if url is None or url not in spec.submodule_mirrors:
        raise EnvSetupError(f"no local mirror for submodule {path} ({url})")
    return REPOS_DIR / f"{spec.submodule_mirrors[url]}.git"


_SETUP_SOURCE = Path(sandbox_setup.__file__).read_text(encoding="utf-8")

_NETWORK_PROBE = r"""
import json, os, socket, subprocess, sys, urllib.request
blocked = {}
try:
    socket.getaddrinfo("github.com", 443); blocked["dns"] = False
except OSError:
    blocked["dns"] = True
try:
    socket.create_connection(("1.1.1.1", 443), timeout=5).close(); blocked["tcp"] = False
except OSError:
    blocked["tcp"] = True
try:
    urllib.request.urlopen("https://pypi.org/simple/", timeout=5); blocked["http"] = False
except Exception:
    blocked["http"] = True
pip = subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps", "--timeout", "5",
                      "--retries", "0", "-d", "/tmp/repogym-pip-probe", "pip"],
                     capture_output=True, timeout=60)
blocked["pip"] = pip.returncode != 0
print(json.dumps({"blocked": blocked, "interfaces": sorted(os.listdir("/sys/class/net"))}))
"""
