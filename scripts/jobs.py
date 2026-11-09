"""Start, stop and inspect long-running jobs safely: by recorded process group, never by pattern.

Every job runs in its own session and process group (``setsid``). Its PID, process group,
command, log and start time go into ``<dir>/<name>.pid.json``. Stopping signals exactly that
process group, after checking that the recorded PID still runs the recorded command (so a
reused PID is never signalled). Containers of episodes that a stop interrupted are removed
by exact name from the run's heartbeats, never by a name pattern. See docs/week3/RUNBOOK.md.

    python3 scripts/jobs.py start NAME --dir DIR [--at 2026-11-10T08:05Z] -- CMD ...
    python3 scripts/jobs.py stop NAME --dir DIR [--run-dir RUN_DIR] [--timeout 60]
    python3 scripts/jobs.py status --dir DIR
    python3 scripts/jobs.py adopt NAME --dir DIR --pid PID   # record a job started earlier
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def cmdline(pid: int) -> list[str] | None:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def alive(info: dict) -> bool:
    """The recorded process still exists AND still runs the recorded command (a scheduled job
    starts as a waiting wrapper and execs into its command, so either form counts)."""
    current = cmdline(info["pid"])
    return current is not None and current in (info["cmdline"], info["command"])


def pid_path(directory: Path, name: str) -> Path:
    return directory / f"{name}.pid.json"


def start(args) -> None:
    command = args.command
    if not command:
        sys.exit("no command given")
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = pid_path(directory, args.name)
    if path.exists() and alive(json.loads(path.read_text())):
        sys.exit(f"{args.name} is already running ({path})")
    target = list(command)
    if args.at:  # wait inside the job itself, so the recorded PID is the job from the start
        due = datetime.fromisoformat(args.at.replace("Z", "+00:00")).timestamp()
        wait = f"import time; time.sleep(max({due} - time.time(), 0))"
        command = ["bash", "-c", 'python3 -c "$0" && exec "$@"', wait, *command]
    log = directory / f"{args.name}.log"
    with log.open("ab") as out:
        proc = subprocess.Popen(
            command,
            stdout=out,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=args.cwd,
        )
    time.sleep(0.5)
    info = {
        "name": args.name,
        "pid": proc.pid,
        "pgid": os.getpgid(proc.pid),
        "cmdline": cmdline(proc.pid) or command,  # what runs now (a waiting wrapper, with --at)
        "command": target,  # what it runs once started
        "log": str(log),
        "started_at": now_iso(),
        "start_at": args.at,
    }
    path.write_text(json.dumps(info, indent=1))
    print(f"started {args.name}: pid {info['pid']}, pgid {info['pgid']}, log {log}")


def adopt(args) -> None:
    directory = Path(args.dir)
    directory.mkdir(parents=True, exist_ok=True)
    current = cmdline(args.pid)
    if current is None:
        sys.exit(f"no process {args.pid}")
    info = {
        "name": args.name,
        "pid": args.pid,
        "pgid": os.getpgid(args.pid),
        "cmdline": current,
        "command": current,
        "log": args.log,
        "started_at": None,
        "adopted_at": now_iso(),
    }
    pid_path(directory, args.name).write_text(json.dumps(info, indent=1))
    print(f"adopted {args.name}: pid {info['pid']}, pgid {info['pgid']}")


def interrupted_containers(run_dir: Path) -> list[str]:
    names = []
    for beat in (run_dir / "heartbeat").glob("*.json"):
        state = json.loads(beat.read_text())
        if state.get("state") == "running" and state.get("container"):
            names.append(state["container"])
    return names


def stop(args) -> None:
    path = pid_path(Path(args.dir), args.name)
    info = json.loads(path.read_text())
    if not alive(info):
        print(f"{args.name}: not running (pid {info['pid']} is gone or runs something else)")
    else:
        os.killpg(info["pgid"], signal.SIGINT)
        deadline = time.time() + args.timeout
        while alive(info) and time.time() < deadline:
            time.sleep(1)
        if alive(info):
            sys.exit(f"{args.name}: still running after SIGINT; check before escalating")
        print(f"{args.name}: stopped (pgid {info['pgid']})")
    info["stopped_at"] = now_iso()
    path.write_text(json.dumps(info, indent=1))
    if args.run_dir:
        for name in interrupted_containers(Path(args.run_dir)):
            subprocess.run(["docker", "rm", "--force", name], capture_output=True)
            print(f"removed container {name} (recorded in the heartbeat)")


def status(args) -> None:
    for path in sorted(Path(args.dir).glob("*.pid.json")):
        info = json.loads(path.read_text())
        state = "running" if alive(info) else "not running"
        print(
            f"{info['name']:28} {state:12} pid {info['pid']:>7} pgid {info['pgid']:>7} "
            f"started {info.get('started_at') or info.get('adopted_at')}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("start")
    p.add_argument("name")
    p.add_argument("--dir", required=True)
    p.add_argument("--at")
    p.add_argument("--cwd", default=str(Path(__file__).resolve().parents[1]))
    p.set_defaults(func=start)
    p = sub.add_parser("stop")
    p.add_argument("name")
    p.add_argument("--dir", required=True)
    p.add_argument("--run-dir")
    p.add_argument("--timeout", type=float, default=60)
    p.set_defaults(func=stop)
    p = sub.add_parser("status")
    p.add_argument("--dir", required=True)
    p.set_defaults(func=status)
    p = sub.add_parser("adopt")
    p.add_argument("name")
    p.add_argument("--dir", required=True)
    p.add_argument("--pid", type=int, required=True)
    p.add_argument("--log")
    p.set_defaults(func=adopt)
    argv = sys.argv[1:]
    command: list[str] = []
    if "--" in argv:  # everything after `--` is the job's command, untouched
        cut = argv.index("--")
        argv, command = argv[:cut], argv[cut + 1 :]
    args = parser.parse_args(argv)
    args.command = command
    args.func(args)


if __name__ == "__main__":
    main()
