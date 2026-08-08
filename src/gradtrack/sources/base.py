"""Shared HTTP plumbing for every source client.

Clients are dumb: they build URLs, respect a rate limit, retry, validate, and hand back
:class:`SourcePosting` objects. They do not classify, filter, or decide whether a role is
still open.

The important thing in this module is :class:`FetchOutcome`. Every fetch records whether it
succeeded and how many rows it returned, and that record is written to the snapshot beside
the postings. ``transform/lifecycle.py`` refuses to mark anything closed for a firm whose
outcome was not a clean, non-empty success — because a failed read and a genuinely empty
board are identical in the postings table and must be distinguished in the metadata.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
    wait_fixed,
)

from gradtrack.config import Config

# A 429 means we are being told to slow down, and the polite floor is a minute regardless of
# what exponential backoff would have chosen.
RATE_LIMITED_BACKOFF_SECONDS = 60.0
MAX_ATTEMPTS = 5


class RateLimiter:
    """Per-host minimum spacing between requests, safe across threads.

    Deliberately a sleep rather than a token bucket. Bursting is exactly the behaviour that
    gets a careers host to block us, and there is no deadline here worth bursting for.
    """

    def __init__(self, requests_per_second: float) -> None:
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next_allowed:
                time.sleep(self._next_allowed - now)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


@dataclass(frozen=True)
class FetchOutcome:
    """What happened when we read one firm's board.

    Attributes:
        firm_id: registry id of the firm.
        platform: the ATS it was read from.
        ok: the request completed and the payload validated.
        row_count: postings returned. Zero with ``ok=True`` is still not evidence of an
            empty board — some tenants return an empty page on a bad site name — so
            lifecycle treats zero as unknown too.
        error: short reason when ``ok`` is false. Written to the health report.
    """

    firm_id: str
    platform: str
    ok: bool
    row_count: int = 0
    error: str = ""

    @property
    def usable_for_closure(self) -> bool:
        """Whether absence from this fetch may be read as a posting having closed."""
        return self.ok and self.row_count > 0

    def as_row(self) -> dict[str, Any]:
        return {
            "firm_id": self.firm_id,
            "platform": self.platform,
            "ok": self.ok,
            "row_count": self.row_count,
            "error": self.error,
        }


def _is_retryable(exc: BaseException) -> bool:
    """Transport errors and 5xx/429 are worth retrying; a 404 is a configuration bug."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


def _wait_strategy(retry_state: Any) -> float:
    exc = retry_state.outcome.exception() if retry_state.outcome else None
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429:
        return wait_fixed(RATE_LIMITED_BACKOFF_SECONDS)(retry_state)
    return wait_exponential(multiplier=1, min=1, max=30)(retry_state)


def build_client(config: Config, *, timeout: float = 45.0) -> httpx.Client:
    """An httpx client carrying the project's User-Agent.

    Every outbound request identifies the project and a contact email. We read several
    hundred careers hosts on a schedule; anonymous traffic at that shape is what earns a
    block, and a host that wants us to stop should be able to say so.
    """
    return httpx.Client(
        headers={
            "User-Agent": config.user_agent,
            "Accept": "application/json, text/plain, */*",
        },
        timeout=timeout,
        follow_redirects=True,
    )


@retry(
    retry=retry_if_exception(_is_retryable),
    stop=stop_after_attempt(MAX_ATTEMPTS),
    wait=_wait_strategy,
    reraise=True,
)
def request_json(
    client: httpx.Client,
    method: str,
    url: str,
    limiter: RateLimiter,
    **kwargs: Any,
) -> Any:
    """Rate-limited, retrying JSON request. Raises on a non-retryable status."""
    limiter.wait()
    response = client.request(method, url, **kwargs)
    response.raise_for_status()
    return response.json()


def get_json(client: httpx.Client, url: str, limiter: RateLimiter, **kwargs: Any) -> Any:
    return request_json(client, "GET", url, limiter, **kwargs)


def post_json(client: httpx.Client, url: str, limiter: RateLimiter, **kwargs: Any) -> Any:
    return request_json(client, "POST", url, limiter, **kwargs)


def first_nonempty(*values: str | None) -> str:
    """First value that is not None or blank, else the empty string."""
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""


def contains_singapore(*values: Iterable[str] | str | None) -> bool:
    """Whether any of the given location strings mentions Singapore.

    Substring matching on purpose. Locations arrive as "Singapore", "Hybrid - Singapore",
    "Remote - Singapore", "Singapore, Singapore" and "APAC (Singapore)" depending on the
    platform, and normalising that zoo is not worth it when the question is only ever
    whether this row belongs in a Singapore tracker.
    """
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            if "singapore" in value.lower():
                return True
            continue
        if any("singapore" in str(item).lower() for item in value):
            return True
    return False
