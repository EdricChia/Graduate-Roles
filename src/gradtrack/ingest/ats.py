"""Ingest job: read every wired firm's board, one platform client at a time.

    uv run python -m gradtrack.ingest.ats                      # every wired firm
    uv run python -m gradtrack.ingest.ats --platform greenhouse
    uv run python -m gradtrack.ingest.ats --firm janestreet
    uv run python -m gradtrack.ingest.ats --all-locations      # skip the Singapore filter

The dispatch table below is the only place that knows which client serves which platform.
There is no per-firm code anywhere in this repo, and adding a company is a row in
``data/firms/registry.csv``.

Every firm produces a :class:`FetchOutcome` whether it succeeded or not, and those outcomes
are written into the snapshot alongside the postings. That is what lets ``lifecycle.py``
tell an empty board from a board it could not read.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import date

import httpx

from gradtrack.config import Config, load_config
from gradtrack.firms import Firm, load_registry
from gradtrack.ingest.snapshot import write_snapshot
from gradtrack.schema import Platform, SourcePosting
from gradtrack.sources import (
    ashby,
    greenhouse,
    lever,
    smartrecruiters,
    successfactors,
    workable,
    workday,
)
from gradtrack.sources.base import FetchOutcome, build_client
from gradtrack.sources.robots import RobotsCache

FetchFn = Callable[..., tuple[list[SourcePosting], FetchOutcome]]

# Phase 3 platforms are documented, unauthenticated GETs. Workday (Phase 4) is neither, and
# is the reason firms are read one platform at a time rather than all at once: it needs a
# much slower rate and a much longer timeout than the rest.
CLIENTS: dict[Platform, FetchFn] = {
    Platform.GREENHOUSE: greenhouse.fetch_firm,
    Platform.LEVER: lever.fetch_firm,
    Platform.ASHBY: ashby.fetch_firm,
    Platform.SMARTRECRUITERS: smartrecruiters.fetch_firm,
    Platform.WORKABLE: workable.fetch_firm,
    Platform.WORKDAY: workday.fetch_firm,
    Platform.SUCCESSFACTORS: successfactors.fetch_firm,
}


def _probe_url(platform: Platform, firm: Firm) -> str:
    """A representative URL this firm's client will request, for the robots check.

    Only the self-hosted platforms are checked. Greenhouse, Lever, Ashby, SmartRecruiters and
    Workable serve every customer from one API host whose robots.txt governs that host, not
    the employer's wishes — and those APIs are documented for exactly this use.
    """
    if platform is Platform.WORKDAY and firm.ats_host:
        return f"https://{firm.ats_host}/wday/cxs/{firm.ats_token}/{firm.board_site}/jobs"
    if platform is Platform.SUCCESSFACTORS and firm.ats_host:
        return f"https://{firm.ats_host}/tile-search-results/"
    if platform is Platform.BROWSER and firm.careers_url:
        return firm.careers_url
    return ""


def fetch_platform(
    client: httpx.Client,
    config: Config,
    platform: Platform,
    firms: list[Firm],
    *,
    singapore_only: bool = True,
) -> tuple[list[SourcePosting], list[FetchOutcome]]:
    """Read every given firm on one platform. Never raises: failures become outcomes."""
    fetch = CLIENTS[platform]
    robots = RobotsCache(client=client, user_agent=config.user_agent)
    postings: list[SourcePosting] = []
    outcomes: list[FetchOutcome] = []
    for firm in firms:
        # `.claude/rules/ingest.md` says every host's robots.txt is honoured. That was true by
        # hand-checking each firm as it was added, which does not survive a registry of
        # several hundred — so it is checked here, on every run, against the URL we are about
        # to request. A refusal is recorded as a failed fetch, which means the lifecycle guard
        # treats the firm's postings as `unknown` rather than closing them.
        probe = _probe_url(platform, firm)
        if probe and not robots.can_fetch(probe):
            outcomes.append(
                FetchOutcome(firm.firm_id, platform.value, False, 0, "robots.txt disallows")
            )
            print(f"  [SKIP] {firm.firm_id:<22} robots.txt disallows {probe}")
            continue
        try:
            firm_postings, outcome = fetch(client, config, firm, singapore_only=singapore_only)
        except Exception as exc:  # noqa: BLE001 - a client bug must not abort the whole run
            postings_count = 0
            outcome = FetchOutcome(
                firm.firm_id,
                platform.value,
                False,
                postings_count,
                f"client raised {type(exc).__name__}: {exc}"[:200],
            )
            firm_postings = []
        postings.extend(firm_postings)
        outcomes.append(outcome)
        flag = "ok " if outcome.ok else "FAIL"
        detail = f" {outcome.error}" if outcome.error else ""
        print(f"  [{flag}] {firm.firm_id:<22} {outcome.row_count:>4} SG postings{detail}")
    return postings, outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest ATS job boards for wired firms")
    parser.add_argument("--platform", default="", help="restrict to one ATS platform")
    parser.add_argument(
        "--exclude",
        default="",
        help="comma-separated platforms to skip; lets the slow Workday leg run on its own schedule",
    )
    parser.add_argument("--firm", default="", help="restrict to one firm_id")
    parser.add_argument(
        "--all-locations",
        action="store_true",
        help="keep non-Singapore postings too (useful when probing a new firm)",
    )
    parser.add_argument("--snapshot-date", default="")
    args = parser.parse_args(argv)

    config = load_config()
    registry = load_registry(config.registry_path)
    snapshot_date = date.fromisoformat(args.snapshot_date) if args.snapshot_date else date.today()

    selected = list(registry.fetchable())
    if args.platform:
        selected = [f for f in selected if f.ats_platform == Platform(args.platform)]
    if args.firm:
        selected = [f for f in selected if f.firm_id == args.firm]
    if args.exclude:
        skip = {p.strip() for p in args.exclude.split(",") if p.strip()}
        selected = [f for f in selected if f.ats_platform and f.ats_platform.value not in skip]

    unsupported = {f.ats_platform for f in selected if f.ats_platform not in CLIENTS}
    if unsupported:
        names = ", ".join(sorted(p.value for p in unsupported if p))
        print(f"skipping {names}: no client wired yet (Phase 4)")
        selected = [f for f in selected if f.ats_platform in CLIENTS]

    if not selected:
        print("no wired firms match the selection")
        return 1

    by_platform: dict[Platform, list[Firm]] = {}
    for firm in selected:
        assert firm.ats_platform is not None  # guaranteed by Firm validation
        by_platform.setdefault(firm.ats_platform, []).append(firm)

    total = 0
    failures = 0
    with build_client(config) as client:
        for platform, firms in sorted(by_platform.items(), key=lambda kv: kv[0].value):
            print(f"{platform.value}: {len(firms)} firm(s)")
            postings, outcomes = fetch_platform(
                client, config, platform, firms, singapore_only=not args.all_locations
            )
            write_snapshot(config, platform.value, snapshot_date, postings, outcomes)
            total += len(postings)
            failures += sum(1 for o in outcomes if not o.ok)

    print(f"\n{total} Singapore postings across {len(selected)} firm(s); {failures} failed")
    # Individual firm failures are expected and handled downstream by the lifecycle guard,
    # so they do not fail the run. The health check is what escalates a persistent failure.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
