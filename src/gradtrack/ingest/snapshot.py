"""Writing and reading the append-only raw snapshot tree.

Layout: ``data/raw/source=<platform>/snapshot_date=<YYYY-MM-DD>/{postings,fetch_outcomes}.parquet``

Two files per partition, and the second one is the point. ``postings.parquet`` says what was
on the board; ``fetch_outcomes.parquet`` says whether we were actually able to look. Without
the second file the two states "this firm has no graduate openings" and "this firm's board
returned a 500" are the same empty result, and the lifecycle pass would read the outage as
eighty roles closing at once.

A rerun on the same day overwrites that day's partition and nothing else. History is never
rewritten — the lifecycle table is derived from it and cannot be reconstructed from anything
else.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import polars as pl

from gradtrack.config import Config
from gradtrack.schema import SourcePosting
from gradtrack.sources.base import FetchOutcome

POSTINGS_FILE = "postings.parquet"
OUTCOMES_FILE = "fetch_outcomes.parquet"

# `extra` carries platform-specific fields with mixed types (MyCareersFuture's
# minimumYearsExperience is an int, isPostedOnBehalf a bool, salary a float). Parquet wants
# one type per column, so the whole dict is stored as a JSON string and parsed in transform.
# The alternative — a column per platform-specific field — makes the raw schema change every
# time a new ATS is wired, which is exactly what append-only storage should not do.
POSTING_COLUMNS = (
    "job_key",
    "firm_id",
    "source_platform",
    "external_id",
    "title",
    "apply_url",
    "location_raw",
    "description_text",
    "posted_date",
    "posted_date_basis",
    "department",
    "employment_type",
    "extra_json",
)


def partition_dir(config: Config, platform: str, snapshot_date: date) -> Path:
    return config.raw_dir / f"source={platform}" / f"snapshot_date={snapshot_date.isoformat()}"


def _posting_row(posting: SourcePosting) -> dict[str, object]:
    return {
        "job_key": posting.job_key,
        "firm_id": posting.firm_id,
        "source_platform": posting.source_platform.value,
        "external_id": posting.external_id,
        "title": posting.title,
        "apply_url": posting.apply_url,
        "location_raw": posting.location_raw,
        "description_text": posting.description_text,
        "posted_date": posting.posted_date,
        "posted_date_basis": (
            posting.posted_date_basis.value if posting.posted_date_basis else None
        ),
        "department": posting.department,
        "employment_type": posting.employment_type,
        "extra_json": json.dumps(posting.extra, ensure_ascii=False, default=str),
    }


def write_snapshot(
    config: Config,
    platform: str,
    snapshot_date: date,
    postings: list[SourcePosting],
    outcomes: list[FetchOutcome],
) -> Path:
    """Write one platform's postings and fetch outcomes into its dated partition.

    Outcomes are written even when there are no postings — especially then. An empty
    partition with no outcomes file is indistinguishable from a run that never happened.
    """
    target = partition_dir(config, platform, snapshot_date)
    target.mkdir(parents=True, exist_ok=True)

    rows = [_posting_row(p) for p in postings]
    frame = (
        pl.DataFrame(rows, schema={c: pl.Utf8 for c in POSTING_COLUMNS} | {"posted_date": pl.Date})
        if rows
        else pl.DataFrame(
            schema={c: (pl.Date if c == "posted_date" else pl.Utf8) for c in POSTING_COLUMNS}
        )
    )
    frame.write_parquet(target / POSTINGS_FILE)

    outcome_frame = pl.DataFrame(
        [o.as_row() for o in outcomes],
        schema={
            "firm_id": pl.Utf8,
            "platform": pl.Utf8,
            "ok": pl.Boolean,
            "row_count": pl.Int64,
            "error": pl.Utf8,
        },
    )
    outcome_frame.write_parquet(target / OUTCOMES_FILE)
    return target


def available_snapshots(config: Config, platform: str) -> list[date]:
    """Every snapshot date on disk for a platform, oldest first."""
    root = config.raw_dir / f"source={platform}"
    if not root.exists():
        return []
    dates: list[date] = []
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("snapshot_date="):
            continue
        try:
            dates.append(date.fromisoformat(child.name.split("=", 1)[1]))
        except ValueError:
            continue
    return sorted(dates)


def read_snapshot(config: Config, platform: str, snapshot_date: date) -> pl.DataFrame:
    path = partition_dir(config, platform, snapshot_date) / POSTINGS_FILE
    if not path.exists():
        return pl.DataFrame(
            schema={c: (pl.Date if c == "posted_date" else pl.Utf8) for c in POSTING_COLUMNS}
        )
    return pl.read_parquet(path)


def read_outcomes(config: Config, platform: str, snapshot_date: date) -> pl.DataFrame:
    path = partition_dir(config, platform, snapshot_date) / OUTCOMES_FILE
    if not path.exists():
        return pl.DataFrame(
            schema={
                "firm_id": pl.Utf8,
                "platform": pl.Utf8,
                "ok": pl.Boolean,
                "row_count": pl.Int64,
                "error": pl.Utf8,
            }
        )
    return pl.read_parquet(path)


def read_snapshot_as_postings(
    config: Config, platform: str, snapshot_date: date
) -> list[SourcePosting]:
    """Rehydrate a partition into :class:`SourcePosting` objects.

    Re-validating on the way out is deliberate. It is cheap, and it means a partition written
    by an older version of the schema fails here rather than halfway through the transform.
    """
    frame = read_snapshot(config, platform, snapshot_date)
    postings: list[SourcePosting] = []
    for row in frame.iter_rows(named=True):
        postings.append(
            SourcePosting(
                firm_id=row["firm_id"],
                source_platform=row["source_platform"],
                external_id=row["external_id"],
                title=row["title"],
                apply_url=row["apply_url"],
                location_raw=row["location_raw"] or "",
                description_text=row["description_text"] or "",
                posted_date=row["posted_date"],
                posted_date_basis=row["posted_date_basis"] or None,
                department=row["department"] or "",
                employment_type=row["employment_type"] or "",
                extra=json.loads(row["extra_json"]) if row["extra_json"] else {},
            )
        )
    return postings


def platforms_on_disk(config: Config) -> list[str]:
    if not config.raw_dir.exists():
        return []
    return sorted(
        child.name.split("=", 1)[1]
        for child in config.raw_dir.iterdir()
        if child.is_dir() and child.name.startswith("source=")
    )
