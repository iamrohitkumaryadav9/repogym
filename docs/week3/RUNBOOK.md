# Week 3 runbook

Operating experiment runs: starting, watching, stopping, and recovering. The decisions behind
these rules are in DECISIONS.md (D11, D16, D17).

## Starting a run (tmux or nohup)

The tree must be committed first: provenance records the commit and whether the tree is dirty.
Test-split runs also need the frozen config and `--final-test-run` (the guard).

```bash
# tmux (survives closing the terminal; reattach with `tmux attach -t e1`)
tmux new -s e1 -d "cd ~/repogym && uv run repogym run --experiment configs/experiments/e1.yaml"

# or nohup, with the process ID written to a file so it can be stopped safely
nohup uv run repogym run --experiment configs/experiments/e1.yaml > data/runs/e1.log 2>&1 &
echo $! > data/runs/e1.pid
```

## Watching

* `uv run repogym status --experiment <file>`: per model, cells done, solves, attempts, requests
  used today against the limit, tokens, budget violations, termination counts, ETA.
* `tail -f data/runs/e1.log`, or `tmux attach -t e1`.

## Stopping (rule)

* **Stop a run only by its recorded process ID or process group.**
  * With nohup: `kill -INT $(cat data/runs/e1.pid)`.
  * With tmux: attach and press Ctrl-C, or `tmux kill-session -t e1` (exact session name).
* **Never use `pkill -f`, `killall`, or `pgrep … | xargs kill`**, or any other name or pattern
  match. A pattern also matches the shell that runs it, and any unrelated process whose command
  line happens to contain the text. On 2026-11-09 a `pkill -f` killed the very shell issuing it.
* Each episode runs in a container named `repogym-ep-<random>`. After a stop, check
  `docker ps --format '{{.Names}} {{.CreatedAt}}'` and remove a leftover by its exact name.

## After a stop, crash or reboot

* Rerun the same `repogym run --experiment …` command. Finished cells are skipped; an
  interrupted episode left no record and reruns as the next attempt. The quota ledger
  (`data/quota/ledger.json`) persists, and is safe across several processes (file lock).
* WSL or Docker restarts kill background jobs; start them again the same way.

## Quota and retries (automatic)

* **Per-minute limits:** the limiter keeps each model under 90% of its RPM and TPM.
* **Daily quota:** quotas reset at midnight Pacific (08:00 UTC in winter, 07:00 UTC in summer).
  If a model's daily quota runs out mid-episode, the call is held in place until the reset plus
  60 s; the episode keeps its sandbox. The hold isn't active time and isn't counted by the
  6-hour elapsed cap (D16, D17).
* **429 and 5xx:** retried with jittered exponential backoff, up to 6 attempts per call; then
  the episode ends as `api_failure`. A cell gets at most 3 `api_failure` attempts, and every
  attempt keeps its own files.
