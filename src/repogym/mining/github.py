"""GitHub REST client with an on-disk response cache and rate-limit handling."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import structlog
from dotenv import load_dotenv

from repogym.config import CACHE_DIR, ROOT

log = structlog.get_logger()

API_ROOT = "https://api.github.com"
JSON_ACCEPT = "application/vnd.github+json"
DIFF_ACCEPT = "application/vnd.github.v3.diff"
_NEXT_LINK = re.compile(r'<([^>]+)>;\s*rel="next"')
_CACHEABLE_ERRORS = {404, 406, 410, 422}
_LOW_REMAINING = 5
_MAX_ATTEMPTS = 6


@dataclass(frozen=True)
class Response:
    status: int
    body: str
    link: str = ""

    def json(self) -> Any:
        return json.loads(self.body)

    @property
    def next_url(self) -> str | None:
        match = _NEXT_LINK.search(self.link)
        return match.group(1) if match else None


def load_token() -> str:
    load_dotenv(ROOT / ".env")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError("GITHUB_TOKEN is not set; add it to .env")
    return token


class GitHubClient:
    """Sequential client: GitHub discourages concurrent requests on one token."""

    def __init__(self, token: str, cache_dir: Path = CACHE_DIR, refresh: bool = False) -> None:
        self._http = httpx.Client(
            timeout=60,
            follow_redirects=True,
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "repogym",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._cache_dir = cache_dir
        self._refresh = refresh
        self.network_requests = 0
        self.cache_hits = 0

    def close(self) -> None:
        self._http.close()

    def _cache_path(self, url: str, accept: str) -> Path:
        digest = hashlib.sha256(f"{accept} {url}".encode()).hexdigest()
        return self._cache_dir / digest[:2] / f"{digest}.json"

    def get(self, url: str, accept: str = JSON_ACCEPT) -> Response:
        if url.startswith("/"):
            url = API_ROOT + url
        path = self._cache_path(url, accept)
        if not self._refresh and path.exists():
            self.cache_hits += 1
            return Response(**json.loads(path.read_text(encoding="utf-8")))

        response = self._fetch(url, accept)
        if response.status < 400 or response.status in _CACHEABLE_ERRORS:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(
                    {"status": response.status, "body": response.body, "link": response.link}
                ),
                encoding="utf-8",
            )
            temp.replace(path)
        return response

    def _fetch(self, url: str, accept: str) -> Response:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                raw = self._http.get(url, headers={"Accept": accept})
            except httpx.TransportError as exc:
                self._backoff(attempt, f"transport error: {type(exc).__name__}")
                continue
            self.network_requests += 1

            wait = self._rate_limit_wait(raw)
            if raw.status_code in (403, 429) and wait is not None:
                self._sleep_for_limit(wait)
                continue
            if raw.status_code >= 500:
                self._backoff(attempt, f"server error {raw.status_code}")
                continue
            if wait is not None:
                self._sleep_for_limit(wait)  # proactive: nearly out, but this response is good
            # Invalid bytes become U+FFFD; such a patch then fails to apply and is logged.
            body = raw.content.decode("utf-8", errors="replace")
            return Response(raw.status_code, body, raw.headers.get("link", ""))
        raise RuntimeError(f"Giving up on {url} after {_MAX_ATTEMPTS} attempts")

    @staticmethod
    def _rate_limit_wait(raw: httpx.Response) -> float | None:
        """Seconds to wait if the response shows we are (nearly) rate limited, else None."""
        retry_after = raw.headers.get("retry-after")
        if retry_after is not None:
            return float(retry_after) + 1
        remaining = raw.headers.get("x-ratelimit-remaining")
        reset = raw.headers.get("x-ratelimit-reset")
        if remaining is not None and reset is not None and int(remaining) <= _LOW_REMAINING:
            return max(float(reset) - time.time(), 0) + 2
        return None

    @staticmethod
    def _sleep_for_limit(seconds: float) -> None:
        log.warning("rate_limited", sleep_seconds=round(seconds))
        time.sleep(seconds)

    @staticmethod
    def _backoff(attempt: int, why: str) -> None:
        delay = 2.0**attempt
        log.warning("github_retry", reason=why, sleep_seconds=delay)
        time.sleep(delay)

    def paginate(self, url: str) -> Iterator[list[dict[str, Any]]]:
        """Yield one page at a time, so callers can stop early."""
        next_url: str | None = url
        while next_url:
            response = self.get(next_url)
            if response.status >= 400:
                raise RuntimeError(f"GET {next_url} returned {response.status}")
            yield response.json()
            next_url = response.next_url
