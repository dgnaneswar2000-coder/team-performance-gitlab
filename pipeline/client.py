"""A small, defensive GitLab HTTP client.

Behaviour observed on the live API while building this:
  * Two different rate limiters answer at random (`throttle_unauthenticated_api`
    with limit 500, and `unauthenticated_traffic_per_ip` reporting "limit 1"
    while "remaining" jumps between 19 and 55). The headers aren't trustworthy
    enough to plan around, so we pace politely and treat 429 + Retry-After as
    the real signal.
  * Pagination is offset-based with X-Next-Page headers. We follow those
    instead of guessing page numbers, and keep each listing small by querying
    one day at a time.
  * 5xx / timeouts / non-JSON bodies happen occasionally: retried with backoff.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import requests

from . import config

log = logging.getLogger(__name__)


class RateLimitExhausted(Exception):
    """A 429 asked us to wait longer than we're willing to inside one run."""


@dataclass
class Page:
    url: str
    page: int
    body: list
    next_page: int | None
    total: int | None


@dataclass
class GitLabClient:
    session: requests.Session = field(default_factory=requests.Session)
    sleep: callable = time.sleep
    clock: callable = time.monotonic
    requests_made: int = 0
    _last_request_at: float = 0.0

    def __post_init__(self):
        self.session.headers.update({"User-Agent": config.USER_AGENT, "Accept": "application/json"})
        if config.GITLAB_TOKEN:
            self.session.headers["PRIVATE-TOKEN"] = config.GITLAB_TOKEN

    def get_list(self, url: str, params: dict) -> Page:
        last_error = None
        for attempt in range(config.MAX_RETRIES):
            self._pace()
            try:
                resp = self.session.get(url, params=params, timeout=config.TIMEOUT_S)
                self.requests_made += 1
            except requests.RequestException as exc:
                last_error = exc
                self._backoff(attempt, f"network error: {exc}")
                continue

            if resp.status_code == 200:
                try:
                    body = resp.json()
                except ValueError:
                    last_error = "invalid JSON"
                    self._backoff(attempt, "200 with invalid JSON body")
                    continue
                if not isinstance(body, list):
                    last_error = f"expected a list, got {type(body).__name__}"
                    self._backoff(attempt, f"200 with unexpected body: {last_error}")
                    continue
                return Page(
                    url=resp.url,
                    page=int(params.get("page", 1)),
                    body=body,
                    next_page=_int_header(resp, "X-Next-Page"),
                    total=_int_header(resp, "X-Total"),
                )

            if resp.status_code == 429:
                wait = _retry_after(resp)
                if wait > config.MAX_RATE_LIMIT_WAIT_S:
                    raise RateLimitExhausted(f"asked to wait {wait:.0f}s")
                log.warning("429 rate limited; waiting %.0fs", wait)
                self.sleep(wait)
                continue

            if resp.status_code >= 500:
                last_error = f"HTTP {resp.status_code}"
                self._backoff(attempt, last_error)
                continue

            resp.raise_for_status()  # other 4xx: retrying won't help

        raise RuntimeError(f"giving up on {url} after {config.MAX_RETRIES} attempts: {last_error}")

    def iter_merged_mrs(self, merged_after: str, merged_before: str):
        """All MRs merged in [merged_after, merged_before), one page at a time."""
        url = f"{config.API_BASE}/projects/{config.PROJECT_ID}/merge_requests"
        page_no = 1
        for _ in range(config.MAX_PAGES_PER_DAY):
            params = {
                "state": "merged", "merged_after": merged_after, "merged_before": merged_before,
                "per_page": config.PER_PAGE, "page": page_no,
                "order_by": "created_at", "sort": "asc",
            }
            page = self.get_list(url, params)
            yield page
            if not page.next_page or not page.body:
                return
            page_no = page.next_page
        log.warning("MAX_PAGES_PER_DAY reached for %s; day may be incomplete", merged_after)

    # -------------------------------------------------------------- helpers
    def _pace(self):
        wait = config.MIN_INTERVAL_S - (self.clock() - self._last_request_at)
        if wait > 0:
            self.sleep(wait)
        self._last_request_at = self.clock()

    def _backoff(self, attempt: int, reason: str):
        delay = config.BACKOFF_BASE_S * (2 ** attempt) + random.uniform(0, 1)
        log.warning("%s; retry %d/%d in %.1fs", reason, attempt + 1, config.MAX_RETRIES, delay)
        self.sleep(delay)


def _int_header(resp, name: str) -> int | None:
    value = (resp.headers.get(name) or "").strip()
    return int(value) if value.isdigit() else None


def _retry_after(resp) -> float:
    value = (resp.headers.get("Retry-After") or "").strip()
    if value.isdigit():
        return float(value)
    reset = (resp.headers.get("RateLimit-Reset") or "").strip()
    if reset.isdigit():
        return max(1.0, int(reset) - time.time())
    return 60.0
