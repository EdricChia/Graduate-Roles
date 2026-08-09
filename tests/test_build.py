"""The transform's input resolution.

One test here, and it guards the failure that costs the most and shows the least: a curated
table that rebuilds cleanly, reports success, and is missing most of its rows.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from gradtrack.config import Config
from gradtrack.ingest.snapshot import write_snapshot
from gradtrack.schema import Platform, SourcePosting
from gradtrack.sources.base import FetchOutcome
from gradtrack.transform.build import latest_snapshot_date, load_all_postings


def _config(tmp_path: Path) -> Config:
    return Config(
        user_agent="gradtrack-tests (test@example.com)",
        contact_email="test@example.com",
        raw_dir=tmp_path / "raw",
        manual_dir=tmp_path / "manual",
        firms_dir=tmp_path / "firms",
        curated_dir=tmp_path / "curated",
        duckdb_path=tmp_path / "curated.duckdb",
    )


def _posting(platform: Platform, external_id: str) -> SourcePosting:
    return SourcePosting(
        firm_id="acme",
        source_platform=platform,
        external_id=external_id,
        title="Graduate Analyst",
        apply_url=f"https://example.com/jobs/{external_id}",
        location_raw="Singapore",
    )


def _write(config: Config, platform: Platform, day: date, external_id: str) -> None:
    write_snapshot(
        config,
        platform.value,
        day,
        [_posting(platform, external_id)],
        [FetchOutcome("acme", platform.value, True, 1, "")],
    )


class TestLoadAllPostings:
    def test_a_platform_not_run_today_keeps_its_last_partition(self, tmp_path: Path) -> None:
        """The legs run on different schedules, so their newest dates differ by design.

        Workday goes at 14:00 SGT and everything else at 17:07, and either can be skipped or
        fail. Resolving every platform to one date meant a platform without a partition for
        it simply vanished: a run of Greenhouse and Phenom alone took the live table from
        4,424 postings to 567 while printing a clean summary. The lifecycle guard does not
        cover this — it stops a posting being *closed* without evidence, and cannot restore a
        row the transform never read.
        """
        config = _config(tmp_path)
        _write(config, Platform.WORKDAY, date(2026, 8, 9), "wd-1")
        _write(config, Platform.GREENHOUSE, date(2026, 8, 10), "gh-1")

        loaded = load_all_postings(config, date(2026, 8, 10))

        assert {p.external_id for p in loaded} == {"wd-1", "gh-1"}

    def test_each_platform_contributes_only_its_newest_partition(self, tmp_path: Path) -> None:
        """Carrying a stale partition forward must not also duplicate the fresh one."""
        config = _config(tmp_path)
        _write(config, Platform.GREENHOUSE, date(2026, 8, 8), "gh-old")
        _write(config, Platform.GREENHOUSE, date(2026, 8, 10), "gh-new")

        loaded = load_all_postings(config, date(2026, 8, 10))

        assert {p.external_id for p in loaded} == {"gh-new"}

    def test_a_partition_after_the_target_date_is_ignored(self, tmp_path: Path) -> None:
        """Replaying an older date must not reach forward into a newer snapshot."""
        config = _config(tmp_path)
        _write(config, Platform.GREENHOUSE, date(2026, 8, 8), "gh-old")
        _write(config, Platform.GREENHOUSE, date(2026, 8, 10), "gh-new")

        loaded = load_all_postings(config, date(2026, 8, 9))

        assert {p.external_id for p in loaded} == {"gh-old"}

    def test_latest_snapshot_date_is_the_newest_across_platforms(self, tmp_path: Path) -> None:
        config = _config(tmp_path)
        _write(config, Platform.WORKDAY, date(2026, 8, 9), "wd-1")
        _write(config, Platform.GREENHOUSE, date(2026, 8, 10), "gh-1")

        assert latest_snapshot_date(config) == date(2026, 8, 10)
