"""`repogym view`: a small local web viewer for runs and ATIF trajectories (standard library).

Lists runs and episodes, and replays a trajectory step by step: each action, its observation,
file diffs, the final reward and the grading flags. Serves only data/runs/, on 127.0.0.1. All
content is inserted as text (never as HTML), because trajectories contain agent output.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from repogym.harness import RUNS_DIR

_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")


def list_runs(root: Path) -> list[dict]:
    runs = []
    for run in sorted(root.glob("*/episodes.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        records = [json.loads(line) for line in run.read_text().splitlines() if line.strip()]
        latest = {r["task_id"]: r for r in records}
        rewards = [r["reward"] for r in latest.values() if r["reward"] is not None]
        runs.append(
            {
                "run_id": run.parent.name,
                "episodes": len(latest),
                "agent": next(iter(latest.values()))["agent"] if latest else "",
                "solved": sum(rewards),
                "graded": len(rewards),
            }
        )
    return runs


def run_episodes(root: Path, run_id: str) -> list[dict]:
    path = root / run_id / "episodes.jsonl"
    latest = {}
    for line in path.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            latest[record["task_id"]] = record
    keys = (
        "task_id",
        "status",
        "reward",
        "steps",
        "flags_raised",
        "done_reason",
        "error",
        "setup_seconds",
        "model_name",
        "tokens",
    )
    return [
        {k: r.get(k) for k in keys} for r in sorted(latest.values(), key=lambda r: r["task_id"])
    ]


def episode(root: Path, run_id: str, task_id: str) -> dict:
    run = root / run_id
    record = {
        r["task_id"]: r
        for r in map(
            json.loads, filter(str.strip, (run / "episodes.jsonl").read_text().splitlines())
        )
    }[task_id]
    trajectory = None
    if record.get("trajectory"):
        trajectory = json.loads((run / record["trajectory"]).read_text())
    diff_path = run / "diffs" / f"{task_id}.diff"
    diff = diff_path.read_text(errors="replace") if diff_path.exists() else ""
    return {"record": record, "trajectory": trajectory, "diff": diff}


class Handler(BaseHTTPRequestHandler):
    root: Path = RUNS_DIR

    def log_message(self, *args) -> None:  # quiet
        pass

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
        )
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data) -> None:
        self._send(200, json.dumps(data).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        parts = [unquote(p) for p in self.path.split("?")[0].strip("/").split("/") if p]
        try:
            if not parts:
                return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            if any(not _SAFE.match(p) for p in parts):
                return self._send(400, b"bad path", "text/plain")
            if parts == ["api", "runs"]:
                return self._json(list_runs(self.root))
            if len(parts) == 3 and parts[:2] == ["api", "run"]:
                return self._json(run_episodes(self.root, parts[2]))
            if len(parts) == 4 and parts[:2] == ["api", "episode"]:
                return self._json(episode(self.root, parts[2], parts[3]))
        except (FileNotFoundError, KeyError):
            return self._send(404, b"not found", "text/plain")
        return self._send(404, b"not found", "text/plain")


def serve(port: int = 8765, root: Path = RUNS_DIR) -> ThreadingHTTPServer:
    handler = type("RunsHandler", (Handler,), {"root": root})
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


PAGE = (Path(__file__).parent / "viewer_page.html").read_text(encoding="utf-8")
