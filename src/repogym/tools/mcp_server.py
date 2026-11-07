"""The RepoGym tools as an MCP server, so any MCP-capable agent can work on a task (design D1).

The server runs on the host. Each tool call goes through the same ``Environment.step`` as every
other agent, so it runs in the episode container as the unprivileged ``agent`` user, with the
same budgets, truncation and trajectory recording. The episode is the normal harness episode
(``run_episode``): setup and leakage checks first, and grading, flags and the ATIF trajectory
after ``submit``.

Run it with ``repogym mcp --task <task_id>`` (stdio transport).
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from repogym.env.environment import Action, Environment, Observation
from repogym.tools.definitions import BY_NAME

StepFn = Callable[[str, dict], Observation]


def build_server(instructions: str, step: StepFn):
    """An MCPServer exposing the six tools; every call is delegated to ``step``."""
    from mcp.server.mcpserver import MCPServer

    server = MCPServer("repogym", instructions=instructions, version="1.0")

    def text(tool: str, args: dict) -> str:
        observation = step(tool, args)
        return observation.text if observation.ok else f"[error] {observation.text}"

    def bash(command: str) -> str:
        return text("bash", {"command": command})

    def view_file(path: str, start_line: int | None = None, end_line: int | None = None) -> str:
        args: dict = {"path": path}
        if start_line is not None:
            args["start_line"] = start_line
        if end_line is not None:
            args["end_line"] = end_line
        return text("view_file", args)

    def str_replace(path: str, old: str, new: str) -> str:
        return text("str_replace", {"path": path, "old": old, "new": new})

    def create_file(path: str, content: str) -> str:
        return text("create_file", {"path": path, "content": content})

    def run_tests(target: str) -> str:
        return text("run_tests", {"target": target})

    def submit() -> str:
        return text("submit", {})

    for fn in (bash, view_file, str_replace, create_file, run_tests, submit):
        server.add_tool(fn, name=fn.__name__, description=BY_NAME[fn.__name__]["description"])
    return server


@dataclass
class BridgeAgent:
    """Stands in for the external MCP client inside ``run_episode``: it takes actions from MCP
    tool calls and hands observations back, until the episode is done."""

    name: str = "mcp-client"
    version: str = "1.0"
    model_name: str | None = None
    actions: queue.Queue = field(default_factory=queue.Queue)
    results: queue.Queue = field(default_factory=queue.Queue)
    ready: threading.Event = field(default_factory=threading.Event)
    finished: threading.Event = field(default_factory=threading.Event)
    first: Observation | None = None

    def run(self, env: Environment, first: Observation) -> None:
        self.first = first
        self.ready.set()
        while True:
            action = self.actions.get()
            if action is None:  # client went away
                return
            observation, done, _ = env.step(action)
            self.results.put(observation)
            if done:
                return

    def step(self, tool: str, args: dict) -> Observation:
        self.actions.put(Action(tool=tool, arguments=args))
        observation = self.results.get()
        if observation.done:
            # Don't answer the final call until the episode is graded and recorded: an MCP
            # client typically shuts the server down as soon as it gets this reply.
            self.finished.wait(timeout=1800)
        return observation


def serve_task(task_id: str, run_dir, budgets, agent_name: str = "mcp-client") -> None:
    """Set up the episode, serve the tools over stdio until submit, then grade and record."""
    from repogym.env.environment import load_task
    from repogym.harness import run_episode

    task = load_task(task_id)
    bridge = BridgeAgent(name=agent_name)
    outcome: dict = {}

    def episode() -> None:
        try:
            outcome["record"] = run_episode(task, bridge, run_dir, budgets)
        finally:
            bridge.ready.set()  # also unblocks the server if setup failed
            bridge.finished.set()

    worker = threading.Thread(target=episode, daemon=True)
    worker.start()
    bridge.ready.wait()
    if bridge.first is None:
        record = outcome.get("record")
        raise SystemExit(f"episode setup failed: {record.error if record else 'unknown'}")
    server = build_server(bridge.first.text, bridge.step)
    try:
        server.run("stdio")
    finally:
        bridge.actions.put(None)
        worker.join(timeout=900)
