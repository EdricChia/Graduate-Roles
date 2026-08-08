"""Snapshot diffing: first_seen, last_seen, and status.

Postings have no lifecycle of their own. No ATS tells us a role closed; it simply stops
appearing. So the lifecycle is *derived* by comparing consecutive raw snapshots, which is why
the raw tree is append-only and why it is the one artifact that cannot be rebuilt.

**Absence is not closure.** This module exists mostly to enforce that. A posting missing from
the latest snapshot is `closed` only when we successfully read that firm's board and it
genuinely was not there. If the fetch failed, or came back empty, the posting is `unknown`
and keeps whatever status it had.

Without the guard, one Workday tenant returning a 500 marks eighty live roles closed and
fires eighty notifications. The finance repo carries the same bug in a quieter form: a
`--skip mas_archive` flag went green in CI while halving a curated table from 1,947 rows to
1,357, because a missing input file and an empty input file were indistinguishable
downstream. Here the difference is recorded at fetch time and respected here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import polars as pl

from gradtrack.schema import Status


@dataclass(frozen=True)
class SnapshotView:
    """One day's raw partition for one platform.

    Attributes:
        snapshot_date: the partition date.
        platform: the ATS the rows came from.
        job_keys: every posting present that day.
        closable_firms: firms whose fetch succeeded *and* returned rows. Only postings whose
            firm is in this set may be marked closed on this date.
        firm_of: job_key to firm_id, so a posting can be matched to its fetch outcome.
    """

    snapshot_date: date
    platform: str
    job_keys: frozenset[str]
    closable_firms: frozenset[str]
    firm_of: dict[str, str]


def build_view(
    snapshot_date: date, platform: str, postings: pl.DataFrame, outcomes: pl.DataFrame
) -> SnapshotView:
    """Turn a raw partition into the form the diff needs."""
    firm_of: dict[str, str] = {}
    if postings.height:
        firm_of = dict(
            zip(postings["job_key"].to_list(), postings["firm_id"].to_list(), strict=True)
        )

    closable: set[str] = set()
    single_sweep: str | None = None
    if outcomes.height:
        for row in outcomes.iter_rows(named=True):
            if row["ok"] and (row["row_count"] or 0) > 0:
                closable.add(row["firm_id"])
        # A sweep source (MyCareersFuture) writes one outcome for the whole run rather than
        # one per firm, because it is queried by search term and not by company. When a
        # platform reports exactly one outcome, that outcome governs every posting from it.
        if outcomes.height == 1:
            only = outcomes.row(0, named=True)
            if only["ok"] and (only["row_count"] or 0) > 0:
                single_sweep = only["firm_id"]

    if single_sweep is not None:
        closable |= set(firm_of.values())

    return SnapshotView(
        snapshot_date=snapshot_date,
        platform=platform,
        job_keys=frozenset(firm_of),
        closable_firms=frozenset(closable),
        firm_of=firm_of,
    )


@dataclass(frozen=True)
class LifecycleRow:
    job_key: str
    first_seen: date
    last_seen: date
    status: Status
    disappeared_on: date | None
    times_seen: int


def compute_lifecycle(views: list[SnapshotView]) -> dict[str, LifecycleRow]:
    """Replay snapshots oldest to newest and derive each posting's lifecycle.

    Args:
        views: one per (platform, date), in any order — they are sorted here.

    Returns:
        job_key to :class:`LifecycleRow`.
    """
    ordered = sorted(views, key=lambda v: (v.snapshot_date, v.platform))

    first_seen: dict[str, date] = {}
    last_seen: dict[str, date] = {}
    times_seen: dict[str, int] = {}
    status: dict[str, Status] = {}
    disappeared: dict[str, date | None] = {}
    # Which platform a key belongs to, so a day's absence is only judged against the
    # platform that was actually read that day. Without this, a day when only Greenhouse ran
    # would consider every Workday posting absent.
    platform_of: dict[str, str] = {}

    for view in ordered:
        for key in view.job_keys:
            platform_of[key] = view.platform
            if key not in first_seen:
                first_seen[key] = view.snapshot_date
                status[key] = Status.OPEN
            elif status.get(key) is Status.CLOSED:
                # Back after having genuinely gone. Worth distinguishing from a fresh
                # posting: a reposted role has an application history and an older origin.
                status[key] = Status.REPOSTED
            elif status.get(key) is Status.UNKNOWN:
                status[key] = Status.OPEN
            last_seen[key] = view.snapshot_date
            times_seen[key] = times_seen.get(key, 0) + 1
            disappeared[key] = None

        for key in list(first_seen):
            if key in view.job_keys or platform_of.get(key) != view.platform:
                continue
            firm = view.firm_of.get(key) or _last_known_firm(ordered, key)
            if firm in view.closable_firms:
                if status.get(key) is not Status.CLOSED:
                    disappeared[key] = view.snapshot_date
                status[key] = Status.CLOSED
            else:
                # The guard. We could not see this firm's board, so we know nothing new.
                if status.get(key) is Status.OPEN:
                    status[key] = Status.UNKNOWN

    return {
        key: LifecycleRow(
            job_key=key,
            first_seen=first_seen[key],
            last_seen=last_seen[key],
            status=status[key],
            disappeared_on=disappeared.get(key),
            times_seen=times_seen.get(key, 0),
        )
        for key in first_seen
    }


def _last_known_firm(views: list[SnapshotView], job_key: str) -> str:
    """The firm a posting belonged to the last time we saw it.

    Needed because a posting absent from today's snapshot has no firm in today's data, and
    the closure decision is made per firm.
    """
    for view in reversed(views):
        firm = view.firm_of.get(job_key)
        if firm:
            return firm
    return ""


def load_history(config) -> list[SnapshotView]:  # noqa: ANN001 - Config, imported lazily below
    """Every partition on disk, as views ready to diff.

    Lives here rather than in ``ingest.snapshot`` to keep the dependency pointing one way:
    transform reads ingest, never the reverse.
    """
    from gradtrack.ingest.snapshot import (
        available_snapshots,
        platforms_on_disk,
        read_outcomes,
        read_snapshot,
    )

    views: list[SnapshotView] = []
    for platform in platforms_on_disk(config):
        for snapshot_date in available_snapshots(config, platform):
            views.append(
                build_view(
                    snapshot_date,
                    platform,
                    read_snapshot(config, platform, snapshot_date),
                    read_outcomes(config, platform, snapshot_date),
                )
            )
    return views


def to_frame(lifecycle: dict[str, LifecycleRow]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            {
                "job_key": row.job_key,
                "first_seen": row.first_seen,
                "last_seen": row.last_seen,
                "status": row.status.value,
                "disappeared_on": row.disappeared_on,
                "times_seen": row.times_seen,
            }
            for row in lifecycle.values()
        ],
        schema={
            "job_key": pl.Utf8,
            "first_seen": pl.Date,
            "last_seen": pl.Date,
            "status": pl.Utf8,
            "disappeared_on": pl.Date,
            "times_seen": pl.Int64,
        },
    )
