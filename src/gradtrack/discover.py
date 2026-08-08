"""Find which ATS a firm runs on, by probing the public board APIs.

    uv run python -m gradtrack.discover                  # every todo firm
    uv run python -m gradtrack.discover --firm optiver
    uv run python -m gradtrack.discover --apply          # write verified hits into registry.csv

Adding several hundred firms by hand means opening several hundred careers pages. Every
Phase 3 platform has a public, unauthenticated board endpoint keyed by a short token, and
that token is usually a predictable transform of the company name — so the cheap thing is to
guess and check.

This is a *probe*, not a scraper: one board index per candidate, at the platform's configured
rate, reading only whether a board exists and what it says about itself. Nothing is written
to the raw tree.

**A hit is a suggestion until it is verified, and verification is the whole difficulty.** The
first version of this module trusted a token that looked like the firm's name, and the first
twelve firms it probed produced two confident, wrong answers:

* `greenhouse/mas` exists and has four jobs. It belongs to **Midwest Applied Solutions**, not
  the Monetary Authority of Singapore.
* `greenhouse/edb` exists, has nineteen jobs, two of them in Singapore, and calls itself
  **"EDB"** — which sails past any name check. Its apply links go to `enterprisedb.com`. It
  is EnterpriseDB, the database company, not Singapore's Economic Development Board.

Both would have been written into the registry, and from then on the tracker would have
reported another company's jobs under a firm the user is watching. That is worse than leaving
the row as `todo`, because a gap is visible and a wrong attribution is not. So a hit is only
auto-applied when the board's *own* self-description — the company name it reports, or the
domain its apply links point at — agrees with the firm we were looking for.

Workable deserves a specific warning: it returns HTTP 200 for essentially any account name
and titlecases the token into a plausible-looking board name, so "goldman-sachs" yields a
board called "Goldman Sachs" with zero jobs. Only the job count distinguishes it from a real
board.
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from rapidfuzz import fuzz

from gradtrack.config import Config, load_config
from gradtrack.firms import Firm, FirmStatus, load_registry
from gradtrack.schema import Platform
from gradtrack.sources.base import RateLimiter, build_client, contains_singapore

PROBES: dict[Platform, str] = {
    Platform.GREENHOUSE: "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    Platform.LEVER: "https://api.lever.co/v0/postings/{token}?mode=json",
    Platform.ASHBY: "https://api.ashbyhq.com/posting-api/job-board/{token}",
    Platform.SMARTRECRUITERS: (
        "https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=10"
    ),
    Platform.WORKABLE: "https://apply.workable.com/api/v1/widget/accounts/{token}",
}

# Hosts that belong to the ATS rather than to the employer. An apply link on one of these
# tells us nothing about who the board belongs to.
ATS_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
    "workable.com",
    "myworkdayjobs.com",
    "icims.com",
    "successfactors.com",
)

# Below four characters, tokens collide with unrelated companies far more often than they
# hit. "mas", "edb", "hdb" and "jtc" all found real boards belonging to somebody else.
MIN_AUTO_APPLY_TOKEN = 4
NAME_MATCH_THRESHOLD = 85
TOKEN_MATCH_THRESHOLD = 90
# A subset match is only believable when the shorter side is long enough to be distinctive.
MIN_SUBSET_MATCH_CHARS = 5

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NOISE = re.compile(
    r"\b(inc|ltd|limited|llc|llp|plc|pte|group|holdings|international|company|co|corp|"
    r"corporation|the|and|of|technologies|technology|global|asia|pacific|singapore)\b"
)


def _norm(value: str) -> str:
    return _NON_ALNUM.sub("", value.lower())


def _denoise(value: str) -> str:
    return _NOISE.sub(" ", value.lower()).strip()


def candidate_tokens(firm: Firm) -> list[str]:
    """Plausible ATS tokens for a firm, most likely first.

    Deliberately few. Exotic tokens — Goldman's own `higher.gs.com`, Workday tenant ids that
    look nothing like the brand — are meant to be found by hand and written into the
    registry, not guessed at here.
    """
    name = firm.firm_name.lower()
    stripped = _denoise(name)
    ordered = [
        firm.firm_id,
        _NON_ALNUM.sub("", stripped),
        _NON_ALNUM.sub("-", stripped).strip("-"),
        _NON_ALNUM.sub("", name),
        stripped.split()[0] if stripped.split() else "",
    ]
    out: list[str] = []
    for token in ordered:
        if token and len(token) >= 3 and token not in out:
            out.append(token)
    return out


@dataclass(frozen=True)
class Hit:
    """A board that exists under a guessed token, and what it says about itself."""

    firm_id: str
    firm_name: str
    platform: Platform
    token: str
    total_jobs: int
    singapore_jobs: int
    board_name: str = ""
    sample_url: str = ""

    @property
    def apply_domain(self) -> str:
        """Registrable-ish host of a sample apply link, empty if it is an ATS host."""
        if not self.sample_url:
            return ""
        host = (urlparse(self.sample_url).hostname or "").lower().removeprefix("www.")
        if any(host.endswith(ats) for ats in ATS_HOSTS):
            return ""
        return host

    def verify(self) -> tuple[bool, str]:
        """Whether ``--apply`` may write this, and the reason either way."""
        if self.total_jobs <= 0:
            return False, "empty board - proves nothing"
        if len(self.token) < MIN_AUTO_APPLY_TOKEN:
            return False, f"token {self.token!r} too short to trust"

        target = _denoise(self.firm_name)

        if self.board_name:
            board = _denoise(self.board_name)
            score = fuzz.token_set_ratio(board, target)
            # token_set_ratio scores a subset as a perfect match, which is right for a board
            # legitimately named "Da Vinci" under "Da Vinci Derivatives" and wrong for a
            # three-letter "EDB" under "Singapore Economic Development Board". Requiring the
            # shorter side to be a real word's length separates them.
            long_enough = min(len(_norm(board)), len(_norm(target))) >= MIN_SUBSET_MATCH_CHARS
            if score >= NAME_MATCH_THRESHOLD and long_enough:
                return True, f"board name {self.board_name!r} matches ({score})"

        domain = self.apply_domain
        if domain:
            core = _norm(domain.rsplit(".", 2)[0])
            score = max(
                fuzz.partial_ratio(core, _norm(target)),
                fuzz.token_set_ratio(core, _norm(target)),
            )
            if score >= NAME_MATCH_THRESHOLD:
                return True, f"apply domain {domain} matches ({score})"
            # A concrete, non-ATS domain that disagrees is positive evidence of the wrong
            # company. This is the EnterpriseDB case and it must be a hard no.
            return False, f"apply domain {domain} is not {self.firm_name!r} ({score})"

        if self.board_name:
            return False, f"board name {self.board_name!r} is not {self.firm_name!r}"

        # Lever and Ashby report neither a company name nor an employer domain, so the only
        # evidence is the token — which we guessed. This is the weakest path and gets the
        # strictest test.
        #
        # `ratio` rather than `token_set_ratio`, and the difference is not academic. A
        # token-set comparison scores a subset as a perfect match, so the first run of this
        # accepted three boards at 100: "applied" for Applied Materials (262 jobs, actually
        # Applied Intuition), "arthur" for Arthur D. Little, and "jump" for Jump Trading.
        # `ratio` penalises the length gap and scores those 61, 63 and 53, while an exact
        # brand like "optiver" or "openai" still scores 100.
        score = fuzz.ratio(_norm(self.token), _norm(target))
        if score >= TOKEN_MATCH_THRESHOLD:
            return True, f"token matches firm name ({score:.0f})"
        return False, f"no company name or employer domain to verify against ({score:.0f})"

    @property
    def verified(self) -> bool:
        return self.verify()[0]


def _probe(
    client: httpx.Client, platform: Platform, token: str, limiter: RateLimiter
) -> tuple[int, int, str, str] | None:
    """Return (total, singapore, board_name, sample_url) if a board exists, else None."""
    limiter.wait()
    try:
        response = client.get(PROBES[platform].format(token=token), timeout=25)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None

    if platform is Platform.GREENHOUSE:
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        sg = [j for j in jobs if contains_singapore((j.get("location") or {}).get("name", ""))]
        first = jobs[0] if jobs else {}
        # company_name and absolute_url are the two fields that make Greenhouse verifiable.
        return len(jobs), len(sg), first.get("company_name") or "", first.get("absolute_url") or ""

    if platform is Platform.LEVER:
        jobs = payload if isinstance(payload, list) else []
        sg = [
            j for j in jobs if contains_singapore((j.get("categories") or {}).get("location", ""))
        ]
        first = jobs[0] if jobs else {}
        return len(jobs), len(sg), "", first.get("applyUrl") or first.get("hostedUrl") or ""

    if platform is Platform.ASHBY:
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        sg = [
            j
            for j in jobs
            if contains_singapore(
                j.get("location", ""),
                [s.get("location", "") for s in (j.get("secondaryLocations") or [])],
            )
        ]
        first = jobs[0] if jobs else {}
        return len(jobs), len(sg), "", first.get("applyUrl") or first.get("jobUrl") or ""

    if platform is Platform.SMARTRECRUITERS:
        content = payload.get("content", []) if isinstance(payload, dict) else []
        total = int(payload.get("totalFound", len(content))) if isinstance(payload, dict) else 0
        sg = [
            j for j in content if contains_singapore((j.get("location") or {}).get("country", ""))
        ]
        name = ""
        if content and isinstance(content[0], dict):
            name = ((content[0].get("company") or {}).get("name")) or ""
        return total, len(sg), name, ""

    if platform is Platform.WORKABLE:
        jobs = payload.get("jobs", []) if isinstance(payload, dict) else []
        sg = [j for j in jobs if contains_singapore(j.get("country", ""), j.get("city", ""))]
        name = payload.get("name", "") if isinstance(payload, dict) else ""
        first = jobs[0] if jobs else {}
        return len(jobs), len(sg), name, first.get("url") or ""

    return None


def build_limiters(config: Config) -> dict[Platform, RateLimiter]:
    """One limiter per platform, shared across every firm in a run.

    Built once and passed down. Creating them per firm — as this did originally — means the
    spacing resets on each company and a 180-firm sweep hammers five hosts as fast as the
    network allows, which is the opposite of what the rate limit is for.
    """
    return {platform: RateLimiter(config.rate_limit_for(platform.value)) for platform in PROBES}


def discover_firm(
    client: httpx.Client,
    config: Config,
    firm: Firm,
    limiters: dict[Platform, RateLimiter] | None = None,
) -> list[Hit]:
    """Probe every platform and candidate token for one firm."""
    limiters = limiters if limiters is not None else build_limiters(config)
    hits: list[Hit] = []
    for platform in PROBES:
        limiter = limiters[platform]
        for token in candidate_tokens(firm):
            result = _probe(client, platform, token, limiter)
            if result is None:
                continue
            total, sg, board_name, sample_url = result
            hit = Hit(
                firm.firm_id, firm.firm_name, platform, token, total, sg, board_name, sample_url
            )
            hits.append(hit)
            # Keep looking on this platform only if the board we found is empty — an empty
            # board is usually a squatted or dormant account, and the real one may be under
            # the next candidate.
            if total > 0:
                break
    return hits


def apply_hits(config: Config, hits: list[Hit]) -> tuple[int, list[str]]:
    """Write verified hits into registry.csv, preserving every other column.

    Only rows still marked ``todo`` are touched: a value a human has already set is never
    overwritten by a guess.
    """
    path = config.registry_path
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    best: dict[str, Hit] = {}
    for hit in hits:
        if not hit.verified:
            continue
        current = best.get(hit.firm_id)
        # Prefer the board with Singapore roles; fall back to the larger board.
        if current is None or (hit.singapore_jobs, hit.total_jobs) > (
            current.singapore_jobs,
            current.total_jobs,
        ):
            best[hit.firm_id] = hit

    applied: list[str] = []
    for row in rows:
        hit = best.get((row.get("firm_id") or "").strip())
        if hit is None or (row.get("status") or "").strip() != FirmStatus.TODO.value:
            continue
        row["ats_platform"] = hit.platform.value
        row["ats_token"] = hit.token
        row["robots_status"] = "allowed"
        row["status"] = FirmStatus.WIRED.value
        note = (row.get("notes") or "").strip()
        stamp = f"discovered {hit.total_jobs} jobs ({hit.singapore_jobs} SG); {hit.verify()[1]}"
        row["notes"] = f"{note}; {stamp}" if note else stamp
        applied.append(f"{hit.firm_id} -> {hit.platform.value}/{hit.token}")

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return len(applied), applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe public ATS boards to identify a firm's ATS")
    parser.add_argument("--firm", default="", help="probe one firm_id instead of all todo rows")
    parser.add_argument("--apply", action="store_true", help="write verified hits to registry.csv")
    parser.add_argument("--limit", type=int, default=0, help="probe at most N firms")
    parser.add_argument(
        "--show-rejected", action="store_true", help="also print boards that failed verification"
    )
    args = parser.parse_args(argv)

    config = load_config()
    registry = load_registry(config.registry_path)

    targets = (
        [registry.by_id(args.firm)]
        if args.firm
        else [f for f in registry if f.status is FirmStatus.TODO]
    )
    if args.limit:
        targets = targets[: args.limit]

    print(f"probing {len(targets)} firm(s) across {len(PROBES)} platforms\n")
    all_hits: list[Hit] = []
    limiters = build_limiters(config)
    with build_client(config, timeout=25) as client:
        for firm in targets:
            for hit in discover_firm(client, config, firm, limiters):
                all_hits.append(hit)
                ok, reason = hit.verify()
                if not ok and not args.show_rejected:
                    continue
                print(
                    f"  {'OK ' if ok else 'no '}{hit.firm_id:<20} {hit.platform.value:<16} "
                    f"{hit.token:<20} {hit.total_jobs:>5}j {hit.singapore_jobs:>3}SG  {reason}"
                )

    verified = [h for h in all_hits if h.verified]
    print(
        f"\n{len(all_hits)} board(s) found over {len(targets)} firms; "
        f"{len(verified)} verified, {len(all_hits) - len(verified)} rejected"
    )
    if args.apply:
        count, applied = apply_hits(config, all_hits)
        for line in applied:
            print(f"  wired {line}")
        print(f"registry updated: {count} row(s) moved from todo to wired")
    elif verified:
        print("re-run with --apply to write these into data/firms/registry.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
