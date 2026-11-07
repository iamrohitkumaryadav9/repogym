"""The MCP server exposes the six tools and delegates every call to the episode (in-process)."""

import asyncio

from repogym.env.environment import Observation
from repogym.tools.definitions import BY_NAME
from repogym.tools.mcp_server import BridgeAgent, build_server


def test_server_lists_the_six_tools_and_delegates_calls():
    calls = []

    def step(tool, args):
        calls.append((tool, args))
        ok = tool != "view_file"
        return Observation(text=f"ran {tool}", ok=ok, done=tool == "submit")

    server = build_server("the issue text", step)

    async def scenario():
        tools = {t.name: t for t in await server.list_tools()}
        assert set(tools) == set(BY_NAME)
        assert tools["bash"].description == BY_NAME["bash"]["description"]
        out = await server.call_tool("bash", {"command": "ls"})
        bad = await server.call_tool("view_file", {"path": "x", "start_line": 3})
        await server.call_tool("submit", {})
        return out, bad

    out, bad = asyncio.run(scenario())
    assert out.content[0].text == "ran bash"
    assert bad.content[0].text.startswith("[error]")
    assert calls == [
        ("bash", {"command": "ls"}),
        ("view_file", {"path": "x", "start_line": 3}),
        ("submit", {}),
    ]
    assert server.instructions == "the issue text"


def test_bridge_agent_passes_actions_to_the_environment_until_done():
    import threading

    class Env:
        def __init__(self):
            self.seen = []

        def step(self, action):
            self.seen.append(action.tool)
            done = action.tool == "submit"
            return Observation(text="ok", done=done), done, {}

    bridge, env = BridgeAgent(), Env()
    worker = threading.Thread(target=bridge.run, args=(env, Observation(text="start")))
    worker.start()
    assert bridge.ready.wait(5) and bridge.first.text == "start"
    assert bridge.step("bash", {"command": "ls"}).text == "ok"

    def finish():
        worker.join(5)
        bridge.finished.set()  # what serve_task does once the episode is recorded

    threading.Thread(target=finish).start()
    bridge.step("submit", {})
    worker.join(5)
    assert not worker.is_alive() and env.seen == ["bash", "submit"]
