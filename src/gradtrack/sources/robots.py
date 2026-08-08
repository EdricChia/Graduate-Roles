"""robots.txt fetching, caching and enforcement.

`.claude/rules/ingest.md` says every host's `robots.txt` is checked and honoured. Until now
that was done by hand, one host at a time, which does not survive a registry of several
hundred firms — and a rule nothing enforces is a rule that quietly stops being true.

Two things here.

:class:`RobotsCache` answers "may we fetch this URL", one fetch per host, cached for the run.
It uses the standard library's parser rather than a hand-rolled matcher, so the path-prefix
semantics are the ones the standard actually specifies.

:func:`disallowed_paths` reads the `Disallow` lines out for a different purpose entirely:
on Workday, they *name the career site*. `dbs.wd3.myworkdayjobs.com/robots.txt` returns
`Disallow: /DBS_Careers/`, and `DBS_Careers` is exactly the site id the CXS endpoint needs.
That turns tenant discovery from brute force into a lookup.

**A fetch failure is not permission.** A host whose `robots.txt` is unreachable returns
``unknown``, and the caller decides — the default at ingest is to allow, matching the
standard, but discovery records it so nothing is wired on an unread file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

_DISALLOW = re.compile(r"^\s*Disallow:\s*(\S+)\s*$", re.I | re.M)


class RobotsVerdict(StrEnum):
    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    # No robots.txt, or it could not be read. The standard treats an absent file as
    # permission; an unreadable one is genuinely unknown and is reported as such.
    UNKNOWN = "unknown"


@dataclass
class RobotsCache:
    """One robots.txt fetch per host, reused for the rest of the run."""

    client: httpx.Client
    user_agent: str = "*"
    _parsers: dict[str, RobotFileParser | None] = field(default_factory=dict)
    _bodies: dict[str, str] = field(default_factory=dict)

    def _load(self, host: str) -> RobotFileParser | None:
        if host in self._parsers:
            return self._parsers[host]
        parser: RobotFileParser | None = None
        body = ""
        try:
            response = self.client.get(f"https://{host}/robots.txt", timeout=20)
            if response.status_code == 200 and "text" in response.headers.get(
                "content-type", "text"
            ):
                body = response.text
                parser = RobotFileParser()
                parser.parse(body.splitlines())
            elif response.status_code in (401, 403):
                # Explicitly withheld. The standard says treat this as full disallow.
                parser = RobotFileParser()
                parser.parse(["User-agent: *", "Disallow: /"])
            elif response.status_code == 404:
                # Absent means unrestricted.
                parser = RobotFileParser()
                parser.parse(["User-agent: *", "Disallow:"])
        except httpx.HTTPError:
            parser = None
        self._parsers[host] = parser
        self._bodies[host] = body
        return parser

    def verdict(self, url: str) -> RobotsVerdict:
        host = (urlparse(url).hostname or "").lower()
        if not host:
            return RobotsVerdict.UNKNOWN
        parser = self._load(host)
        if parser is None:
            return RobotsVerdict.UNKNOWN
        return (
            RobotsVerdict.ALLOWED
            if parser.can_fetch(self.user_agent, url)
            else RobotsVerdict.DISALLOWED
        )

    def can_fetch(self, url: str, *, unknown_is_allowed: bool = True) -> bool:
        verdict = self.verdict(url)
        if verdict is RobotsVerdict.UNKNOWN:
            return unknown_is_allowed
        return verdict is RobotsVerdict.ALLOWED

    def crawl_delay(self, host: str) -> float | None:
        parser = self._load(host)
        if parser is None:
            return None
        delay = parser.crawl_delay(self.user_agent)
        return float(delay) if delay is not None else None

    def body(self, host: str) -> str:
        self._load(host)
        return self._bodies.get(host, "")


def disallowed_paths(body: str) -> list[str]:
    """Every path named in a `Disallow` line, in order, ignoring the blanket ones."""
    return [
        path
        for path in _DISALLOW.findall(body)
        if path not in {"/", "*"} and not path.startswith("/*")
    ]


# Workday puts operational paths in robots.txt alongside the career sites. These are never
# site ids.
_WORKDAY_NON_SITES = {
    "refreshfacet",
    "wday",
    "talentcommunity",
    "talent_community",
    "static",
    "assets",
}


def workday_site_candidates(body: str) -> list[str]:
    """Career-site ids named in a Workday tenant's robots.txt.

    `Disallow: /DBS_Careers/` means the site id is `DBS_Careers`. Not every tenant lists one
    — Salesforce and Micron disallow nothing — so this narrows the search rather than ending
    it.
    """
    names: list[str] = []
    for path in disallowed_paths(body):
        first = path.strip("/").split("/")[0]
        if first and first.lower() not in _WORKDAY_NON_SITES and first not in names:
            names.append(first)
    return names
