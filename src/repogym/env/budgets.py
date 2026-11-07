"""Per-episode budgets and observation truncation, enforced on the host."""

from __future__ import annotations

from pydantic import BaseModel


class Budgets(BaseModel):
    max_steps: int = 100
    max_wall_seconds: float = 1800
    max_total_observation_chars: int = 1_000_000
    command_timeout_seconds: float = 120
    run_tests_timeout_seconds: float = 300
    max_observation_chars: int = 10_000
    max_cost_usd: float | None = None
    # Disk: the container's writable layer is measured after every step (docker inspect --size,
    # ~0.06 s); over the cap ends the episode. Each command also has a per-file size limit, so
    # one command can't fill the disk before the next check. (A read-only root was evaluated
    # and rejected: see docs/design/week2_environment.md, D7.)
    max_disk_mb: int = 2048
    max_file_mb: int = 1024


def truncate(text: str, limit: int) -> tuple[str, bool]:
    """Keep the first and last halves of ``text`` with a marker saying how much was removed."""
    if len(text) <= limit:
        return text, False
    marker_room = 80
    keep = max(limit - marker_room, 0)
    head, tail = keep // 2, keep - keep // 2
    removed = len(text) - head - tail
    marker = f"\n[... {removed} characters truncated ...]\n"
    return text[:head] + marker + (text[-tail:] if tail else ""), True
