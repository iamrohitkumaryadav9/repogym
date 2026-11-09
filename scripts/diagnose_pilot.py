"""Diagnose why pilot episodes hit the step cap. Read-only: trajectories, records, diffs, tasks."""

import collections
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "data/runs/week3-pilot"
TASKS = {
    json.loads(l)["task_id"]: json.loads(l)
    for l in (ROOT / "data/release/v0.1/tasks.jsonl").read_text().splitlines()
}
EDIT_BASH = re.compile(r"sed -i|>\s*\S+\.py|tee |patch |python3? -c .*open\(.*['\"]w|cat >")
CLIP_CHARS = 4700  # ~ what the 2,000-token clip keeps at the starting estimate


def norm(tool, args):
    if tool == "bash":
        return ("bash", re.sub(r"\s+", " ", args.get("command", "")).strip())
    if tool in ("view_file", "create_file"):
        return (tool, args.get("path"), args.get("start_line"), args.get("end_line"))
    if tool == "str_replace":
        return (tool, args.get("path"), args.get("old_str", "")[:200], args.get("new_str", "")[:200])
    return (tool, json.dumps(args, sort_keys=True)[:200])


def path_of(tool, args):
    if tool in ("view_file", "str_replace", "create_file"):
        return args.get("path")
    if tool == "bash":
        m = re.match(r"\s*(?:cat|head|tail|sed -n \S+|nl)\s+(?:-\S+\s+)*(\S+\.py)", args.get("command", ""))
        return m.group(1) if m else None
    return None


def gold_files(task):
    patch = task.get("patch") or task.get("gold_patch") or ""
    return sorted(set(re.findall(r"^diff --git a/(\S+)", patch, re.M)))


def analyse(key, record):
    doc = json.loads((RUN / record["trajectory"]).read_text())
    steps = [s for s in doc["steps"] if s["source"] == "agent"]
    rows, unit = [], 0
    for i, s in enumerate(steps, 1):
        rep = (s.get("metrics") or {}).get("extra", {}).get("repogym", {})
        if s.get("metrics"):
            unit += 1
        call = (s.get("tool_calls") or [{}])[0]
        tool, args = call.get("function_name"), call.get("arguments") or {}
        x = s.get("extra", {}).get("repogym", {})
        obs = ((s.get("observation") or {}).get("results") or [{}])[0].get("content", "")
        rows.append(dict(i=i, unit=unit, tool=tool, args=args, ok=x.get("ok"), exit=x.get("exit_code"),
                         raw=x.get("raw_chars") or len(obs), obs=obs, view=rep.get("view") or {},
                         text=s.get("message") or ""))
    tools = collections.Counter(r["tool"] for r in rows)
    edits = [r for r in rows if r["tool"] in ("str_replace", "create_file") or
             (r["tool"] == "bash" and EDIT_BASH.search(r["args"].get("command", "")))]
    ok_edits = [r for r in edits if r["ok"]]
    failed_sr = [r for r in rows if r["tool"] == "str_replace" and not r["ok"]]
    errors = [r for r in rows if r["ok"] is False]
    # repetition
    seen, repeats, first_repeat = collections.Counter(), 0, None
    for r in rows:
        k = norm(r["tool"], r["args"])
        if seen[k]:
            repeats += 1
            first_repeat = first_repeat or r["i"]
        seen[k] += 1
    top = [(n, k) for k, n in seen.most_common(4) if n > 1]
    # longest run of the same (tool, path/command) in consecutive steps
    # re-reads after masking: path read at unit u, masked in a later view, then read again
    reads = collections.defaultdict(list)
    reread_after_mask = []
    for r in rows:
        p = path_of(r["tool"], r["args"])
        if p and r["tool"] != "str_replace":
            for u in reads[p]:
                if r["view"].get("units", {}).get(str(u)) == "masked":
                    reread_after_mask.append((r["i"], p))
                    break
            reads[p].append(r["unit"])
    rt = [r for r in rows if r["tool"] == "run_tests"]
    rt_clipped = [r for r in rt if r["raw"] > CLIP_CHARS]
    no_tool = [r for r in rows if r["tool"] is None]
    task = TASKS[record["task_id"]]
    stem = Path(record["trajectory"]).stem  # <cell>__a<n> since attempts are numbered
    diff_path = RUN / "diffs" / f"{stem}.diff"
    diff = diff_path.read_text() if diff_path.exists() else ""
    diff_files = sorted(set(re.findall(r"^diff --git a/(\S+)", diff, re.M)))
    g = record.get("grade") or {}
    out = {
        "key": key, "termination": record["termination"], "reward": record["reward"],
        "steps": len(rows), "tools": dict(tools),
        "first_edit_step": edits[0]["i"] if edits else None,
        "edits": len(edits), "ok_edits": len(ok_edits), "failed_str_replace": len(failed_sr),
        "errors": len(errors), "no_tool_turns": len(no_tool),
        "run_tests": len(rt), "run_tests_clipped": len(rt_clipped), "submit": tools.get("submit", 0),
        "repeat_actions": repeats, "first_repeat_step": first_repeat,
        "top_repeats": [(n, str(k)[:110]) for n, k in top],
        "rereads_after_mask": len(reread_after_mask), "first_reread_after_mask": reread_after_mask[:5],
        "diff_files": diff_files, "gold_files": gold_files(task), "diff_lines": diff.count("\n"),
        "grade": {k: v for k, v in g.items() if k in ("status", "fail_to_pass", "pass_to_pass",
                                                      "f2p_passed", "p2p_passed", "passed", "failed",
                                                      "summary", "total")},
        "accounting": {k: (record.get("accounting") or {}).get(k) for k in
                       ("calls", "input_tokens", "budget_violations", "estimation_error_mean",
                        "waiting_seconds", "summary_omitted", "episode_seconds")},
    }
    return out, rows


def timeline(rows, every=1):
    for r in rows[::every]:
        a = r["args"]
        desc = a.get("command") or a.get("path") or json.dumps(a)[:80]
        if r["tool"] == "str_replace":
            desc = f'{a.get("path")} old={a.get("old_str","")[:40]!r}'
        print(f'{r["i"]:3} u{r["unit"]:<3} {str(r["tool"]):12} ok={str(r["ok"])[0]} '
              f'raw={r["raw"]:6} {str(desc)[:95]!s}')
        if r["ok"] is False:
            print("        !", r["obs"][:160].replace("\n", " | "))


if __name__ == "__main__":
    records = [json.loads(l) for l in (RUN / "episodes.jsonl").read_text().splitlines()]
    want = sys.argv[1] if len(sys.argv) > 1 else ""
    for rec in records:
        if rec["termination"] == "api_failure" and not want:
            continue
        if want and want not in rec["episode_key"]:
            continue
        out, rows = analyse(rec["episode_key"], rec)
        print(json.dumps(out, indent=1, default=str))
        if want:
            timeline(rows)
