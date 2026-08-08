"""Ingest job: sweep MyCareersFuture into today's raw partition.

    uv run python -m gradtrack.ingest.mcf
    uv run python -m gradtrack.ingest.mcf --terms graduate,quantitative --max-pages 1

Fetches and writes. It does not classify, filter to graduate roles, or decide which firm a
posting belongs to — all of that is `transform/`.
"""

from __future__ import annotations

import argparse
from datetime import date

from gradtrack.config import load_config
from gradtrack.ingest.snapshot import write_snapshot
from gradtrack.schema import Platform
from gradtrack.sources import mcf
from gradtrack.sources.base import build_client


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest MyCareersFuture postings")
    parser.add_argument(
        "--terms",
        default="",
        help="comma-separated search terms; defaults to the full sweep in sources.mcf",
    )
    parser.add_argument("--max-pages", type=int, default=mcf.MAX_PAGES_PER_TERM)
    parser.add_argument(
        "--snapshot-date",
        default="",
        help="YYYY-MM-DD; defaults to today. Writing an older date rewrites that partition.",
    )
    args = parser.parse_args(argv)

    config = load_config()
    snapshot_date = date.fromisoformat(args.snapshot_date) if args.snapshot_date else date.today()
    terms = (
        tuple(t.strip() for t in args.terms.split(",") if t.strip())
        if args.terms
        else mcf.SEARCH_TERMS
    )

    print(f"MyCareersFuture sweep: {len(terms)} terms, max {args.max_pages} pages each")
    with build_client(config) as client:
        postings, outcome = mcf.fetch_all(client, config, terms=terms, max_pages=args.max_pages)

    target = write_snapshot(config, Platform.MCF.value, snapshot_date, postings, [outcome])
    print(f"  {outcome.row_count} unique postings -> {target}")
    if outcome.error:
        print(f"  partial failures: {outcome.error}")
    # A sweep that returned nothing is a failure worth a non-zero exit: it is the shape a
    # silently-dead source takes, and CI has to be able to see it.
    return 0 if outcome.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
