"""The agent's tools: names, descriptions and JSON-schema parameters.

One definition list serves the environment (argument validation), the MCP server, and ATIF's
``agent.tool_definitions`` (OpenAI function-calling style).
"""

from __future__ import annotations

TOOLS: list[dict] = [
    {
        "name": "bash",
        "description": (
            "Run a bash command in the repository directory (/work) and return its exit code, "
            "stdout and stderr. Commands are killed after the per-command timeout. There is no "
            "network access."
        ),
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command to run."}},
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "view_file",
        "description": (
            "Show a file with line numbers. Paths are relative to /work. At most 150 lines per "
            "call; use start_line and end_line to page through longer files."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1},
                "end_line": {"type": "integer", "minimum": 1},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "str_replace",
        "description": (
            "Replace one exact occurrence of `old` with `new` in a file. `old` must match "
            "exactly once, including whitespace and indentation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old": {"type": "string"},
                "new": {"type": "string"},
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_file",
        "description": (
            "Create a file (or overwrite it) with the given content. Paths are under /work."
        ),
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_tests",
        "description": (
            "Run tests with pytest. `target` is a test file or node id (path::test_name) that "
            "exists in the repository at the start of the task. Use bash to run tests you add."
        ),
        "parameters": {
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
            "additionalProperties": False,
        },
    },
    {
        "name": "submit",
        "description": "Finish the task. Your changes to /work are graded after this.",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]

BY_NAME = {tool["name"]: tool for tool in TOOLS}


def validate_arguments(name: str, arguments: object) -> str | None:
    """None if valid, else an informative error message (returned to the agent)."""
    tool = BY_NAME.get(name)
    if tool is None:
        return f"unknown tool {name!r}; available tools: {', '.join(BY_NAME)}"
    if not isinstance(arguments, dict):
        return f"{name}: arguments must be a JSON object"
    schema = tool["parameters"]
    missing = [key for key in schema.get("required", []) if key not in arguments]
    if missing:
        return f"{name}: missing required argument(s): {', '.join(missing)}"
    unknown = [key for key in arguments if key not in schema["properties"]]
    if unknown:
        return f"{name}: unknown argument(s): {', '.join(unknown)}"
    for key, value in arguments.items():
        expected = schema["properties"][key]["type"]
        ok = (
            isinstance(value, str)
            if expected == "string"
            else (isinstance(value, int) and not isinstance(value, bool))
        )
        if not ok:
            return f"{name}: argument {key!r} must be a {expected}"
        if expected == "integer" and value < schema["properties"][key].get("minimum", value):
            return f"{name}: argument {key!r} must be >= {schema['properties'][key]['minimum']}"
    return None


def openai_style() -> list[dict]:
    """ATIF ``tool_definitions`` / OpenAI function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["parameters"],
            },
        }
        for t in TOOLS
    ]
