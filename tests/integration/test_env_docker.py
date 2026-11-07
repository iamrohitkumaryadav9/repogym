"""Integration tests: real containers, real bare clones. Run with `pytest -m docker`."""

import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

import pytest

from repogym.config import get_repo
from repogym.env.budgets import Budgets
from repogym.env.environment import Action, Environment, load_task
from repogym.env.extract import _ENV, _SAFE, _skip
from repogym.env.sandbox import _docker
from repogym.env.sandbox_setup import WORK

pytestmark = pytest.mark.docker
TASK = "python-poetry__tomlkit-537"  # has a submodule (tests/toml-test) and a released copy


@pytest.fixture
def env():
    environment = Environment(load_task(TASK), Budgets())
    environment.reset()
    yield environment
    environment.close()


def root_tree(env) -> dict[str, tuple]:
    """/work as the container sees it, read by a separate root exec that only reads (tar)."""
    done = _docker("exec", "-u", "0", env.sandbox.name, "tar", "-C", WORK, "-cf", "-", ".")
    tree = {}
    with tarfile.open(fileobj=io.BytesIO(done.stdout), mode="r:") as tar:
        for m in tar:
            rel = m.name[2:] if m.name.startswith("./") else m.name
            if not rel or rel == ".":
                continue
            if m.isfile():
                tree[rel] = ("file", tar.extractfile(m).read(), bool(m.mode & 0o111))
            elif m.issym():
                tree[rel] = ("link", m.linkname)
    return tree


def rebuilt_tree(env, diff: str) -> dict[str, tuple]:
    """base_commit plus the extracted diff, on the host: what grading starts from."""
    spec = get_repo(env.task.repo)
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "r"
        subprocess.run(
            [
                "git",
                *_SAFE,
                "clone",
                "-q",
                "--no-checkout",
                "--shared",
                str(spec.bare_path),
                str(repo),
            ],
            env=_ENV,
            check=True,
        )
        subprocess.run(
            ["git", *_SAFE, "checkout", "-q", "--detach", env.task.base_commit],
            cwd=repo,
            env=_ENV,
            check=True,
        )
        if diff:
            subprocess.run(
                ["git", *_SAFE, "apply", "--binary", "-"],
                cwd=repo,
                env=_ENV,
                input=diff.encode("utf-8", "surrogateescape"),
                check=True,
            )
        tree = {}
        for path in repo.rglob("*"):
            rel = path.relative_to(repo).as_posix()
            if rel.startswith(".git/") or rel == ".git":
                continue
            if path.is_symlink():
                tree[rel] = ("link", str(Path(path).readlink()))
            elif path.is_file():
                tree[rel] = ("file", path.read_bytes(), bool(path.stat().st_mode & 0o111))
        return tree


def test_extracted_tree_matches_the_agents_final_work_byte_for_byte(env):
    steps = [
        ("create_file", {"path": "tomlkit/new_module.py", "content": "VALUE = 'é ✓'\n"}),
        (
            "str_replace",
            {
                "path": "tomlkit/__init__.py",
                "old": "__version__ = ",
                "new": "__version__  =  ",
            },
        ),
        ("bash", {"command": "printf '\\x00\\x01\\xff binary' > tomlkit/blob.bin"}),
        ("bash", {"command": "printf '#!/bin/sh\\necho hi\\n' > run.sh && chmod +x run.sh"}),
        ("bash", {"command": "ln -s tomlkit/api.py api_link.py"}),
        ("bash", {"command": "git rm -q --cached README.md 2>/dev/null; rm -f README.md"}),
        ("bash", {"command": "printf 'crlf\\r\\nline\\r\\n' > crlf.txt"}),
        ("bash", {"command": "git add -A && git commit -qm agent-commit"}),
    ]
    for tool, args in steps:
        obs, _, _ = env.step(Action(tool=tool, arguments=args))
        assert obs.ok or tool == "bash", obs.text
    actual = root_tree(env)
    work = env.collect()
    assert work.committed
    rebuilt = rebuilt_tree(env, work.diff)
    artifacts = set(env.setup_report.setup_artifacts)
    gitlinks = {"tests/toml-test"}
    expected = {k: v for k, v in actual.items() if not _skip(k, artifacts, gitlinks)}
    got = {k: v for k, v in rebuilt.items() if not _skip(k, artifacts, gitlinks)}
    assert set(got) == set(expected), set(got) ^ set(expected)
    for path in expected:
        assert got[path] == expected[path], path
    assert "README.md" not in got and got["api_link.py"] == ("link", "tomlkit/api.py")
    assert got["run.sh"][2] is True and got["tomlkit/blob.bin"][1].startswith(b"\x00\x01\xff")


def test_planted_package_copy_and_metadata_are_detected(env):
    from repogym.env.sandbox import _SETUP_SOURCE

    name = env.sandbox.name
    _docker(
        "exec",
        "-u",
        "0",
        name,
        "cp",
        "-r",
        "/work/tomlkit",
        "/usr/local/lib/python3.11/site-packages/tomlkit",
    )
    code = _SETUP_SOURCE.replace('if __name__ == "__main__":\n    main()', "") + (
        "\nimport json,sys\nprint(json.dumps(scan_copies(json.loads(sys.argv[1]))))"
    )
    out = _docker(
        "exec",
        "-u",
        "0",
        name,
        "python3",
        "-c",
        code,
        '{"packages": ["tomlkit"], "dists": ["tomlkit"]}',
    ).stdout.decode()
    assert "site-packages/tomlkit" in out
    _docker("exec", "-u", "0", name, "sh", "-c", f"echo {TASK} > /etc/leak.txt")
    checks = env.sandbox._check_metadata(type("R", (), {})())
    assert not checks[0].ok and "/etc/leak.txt" in checks[0].detail


def test_file_tools_refuse_symlinks_out_of_work(env):
    env.step(
        Action(tool="bash", arguments={"command": "ln -s /etc escape && ln -s /etc/passwd pw"})
    )
    for tool, args in [
        ("view_file", {"path": "pw"}),
        ("create_file", {"path": "escape/evil", "content": "x"}),
        ("str_replace", {"path": "pw", "old": "root", "new": "toor"}),
    ]:
        obs, _, _ = env.step(Action(tool=tool, arguments=args))
        assert not obs.ok and "outside /work" in obs.text, obs.text


def test_agent_runs_unprivileged_and_offline(env):
    obs, _, _ = env.step(
        Action(
            tool="bash",
            arguments={
                "command": "id -u; touch /usr/local/lib/python3.11/site-packages/x 2>&1; "
                "python -c \"import socket; socket.create_connection(('1.1.1.1', 53), 3)\" "
                "2>&1 | tail -1"
            },
        )
    )
    assert obs.text.splitlines()[1] == "1000"
    assert "Permission denied" in obs.text and "unreachable" in obs.text.lower()
