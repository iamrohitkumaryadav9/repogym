"""File tools. Runs INSIDE the agent container as the ``agent`` user; standard library only.

Reads one JSON request from stdin and prints one JSON reply. Every path is resolved with
``realpath`` (so symlinks are followed) and must stay inside /work, or the call is refused.
"""

import json
import os
import sys

WORK = "/work"
MAX_VIEW_LINES = 150  # about what the Week 3 prompt clip keeps (D19)
MAX_FILE_BYTES = 5_000_000


class ToolError(Exception):
    pass


def resolve(path):
    """Absolute, symlink-free path inside /work, or ToolError. Works for not-yet-existing paths."""
    if not isinstance(path, str) or not path.strip():
        raise ToolError("path must be a non-empty string")
    if "\0" in path:
        raise ToolError("path contains a NUL byte")
    joined = os.path.normpath(path if os.path.isabs(path) else os.path.join(WORK, path))
    # realpath of the deepest existing ancestor, then the rest appended, so a symlinked parent
    # directory pointing outside /work is caught before anything is created.
    head, tail = joined, []
    while not os.path.lexists(head) and head != "/":
        head, name = os.path.split(head)
        tail.insert(0, name)
    real = os.path.realpath(os.path.join(os.path.realpath(head), *tail))
    if real != WORK and not real.startswith(WORK + "/"):
        raise ToolError(
            f"{path!r} resolves to {real}, outside /work; file tools only work in /work"
        )
    return real


def rel(real):
    return os.path.relpath(real, WORK)


def read_text(real):
    if not os.path.exists(real):
        raise ToolError(f"{rel(real)}: no such file")
    if os.path.isdir(real):
        raise ToolError(f"{rel(real)} is a directory; use bash `ls` to list it")
    if os.path.getsize(real) > MAX_FILE_BYTES:
        raise ToolError(f"{rel(real)} is larger than {MAX_FILE_BYTES} bytes")
    with open(real, "rb") as handle:
        data = handle.read()
    if b"\0" in data:
        raise ToolError(f"{rel(real)} looks binary (contains NUL bytes); not shown")
    return data.decode("utf-8", errors="replace")


def view_file(req):
    real = resolve(req.get("path"))
    lines = read_text(real).splitlines()
    total = len(lines)
    start = req.get("start_line") or 1
    end = req.get("end_line") or min(total, start + MAX_VIEW_LINES - 1)
    if not isinstance(start, int) or not isinstance(end, int):
        raise ToolError("start_line and end_line must be integers")
    if total == 0:
        return f"{rel(real)} is empty (0 lines)"
    if start < 1 or start > total:
        raise ToolError(f"start_line {start} is out of range: {rel(real)} has {total} lines")
    if end < start:
        raise ToolError(f"end_line {end} is before start_line {start}")
    end = min(end, total)
    if end - start + 1 > MAX_VIEW_LINES:
        raise ToolError(
            f"range {start}-{end} is {end - start + 1} lines; at most {MAX_VIEW_LINES} per call"
        )
    body = "\n".join(f"{n:>6}\t{lines[n - 1]}" for n in range(start, end + 1))
    return f"{rel(real)} (lines {start}-{end} of {total})\n{body}"


def str_replace(req):
    real = resolve(req.get("path"))
    old, new = req.get("old"), req.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        raise ToolError("old and new must be strings")
    if old == "":
        raise ToolError("old must not be empty; use create_file to write a whole file")
    if old == new:
        raise ToolError("old and new are identical; nothing to change")
    text = read_text(real)
    count = text.count(old)
    if count == 0:
        raise ToolError(
            f"old text not found in {rel(real)}. It must match exactly, including whitespace "
            "and indentation; view the file to copy it"
        )
    if count > 1:
        starts, at = [], 0
        for _ in range(count):
            at = text.index(old, at)
            starts.append(text.count("\n", 0, at) + 1)
            at += 1
        raise ToolError(
            f"old text matches {count} times in {rel(real)} (starting at lines "
            f"{', '.join(map(str, starts[:10]))}); include more surrounding lines to make it unique"
        )
    line = text.count("\n", 0, text.index(old)) + 1
    with open(real, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old, new, 1))
    return f"replaced 1 occurrence in {rel(real)} at line {line}"


def create_file(req):
    real = resolve(req.get("path"))
    content = req.get("content")
    if not isinstance(content, str):
        raise ToolError("content must be a string")
    if os.path.isdir(real):
        raise ToolError(f"{rel(real)} is a directory")
    existed = os.path.exists(real)
    os.makedirs(os.path.dirname(real), exist_ok=True)
    with open(real, "w", encoding="utf-8") as handle:
        handle.write(content)
    action = "overwrote" if existed else "created"
    return f"{action} {rel(real)} ({len(content.splitlines())} lines)"


OPS = {"view_file": view_file, "str_replace": str_replace, "create_file": create_file}


def main():
    req = json.load(sys.stdin)
    try:
        op = OPS.get(req.get("op"))
        if op is None:
            raise ToolError(f"unknown file operation {req.get('op')!r}")
        print(json.dumps({"ok": True, "output": op(req)}))
    except ToolError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
    except OSError as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc.strerror or exc}"}))


if __name__ == "__main__":
    main()
