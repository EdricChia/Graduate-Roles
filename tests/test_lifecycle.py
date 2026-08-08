"""Lifecycle diffing. The other file that must always pass.

The test that matters most is `test_a_failed_fetch_does_not_close_anything`. Everything else
here is bookkeeping; that one is the difference between a tracker and eighty false
notifications on the morning a Workday tenant has an outage.
"""

from __future__ import annotations

from datetime import date

import polars as pl

from gradtrack.ingest.snapshot import OUTCOMES_FILE, POSTINGS_FILE, write_snapshot
from gradtrack.schema import Platform, PostedDateBasis, SourcePosting, Status
from gradtrack.sources.base import FetchOutcome
from gradtrack.transform.lifecycle import build_view, compute_lifecycle

D1, D2, D3 = date(2026, 8, 1), date(2026, 8, 2), date(2026, 8, 3)


def _postings(*pairs: tuple[str, str]) -> pl.DataFrame:
    """pairs of (job_key, firm_id)."""
    return pl.DataFrame(
        {"job_key": [p[0] for p in pairs], "firm_id": [p[1] for p in pairs]},
        schema={"job_key": pl.Utf8, "firm_id": pl.Utf8},
    )


def _outcomes(*rows: tuple[str, bool, int]) -> pl.DataFrame:
    """rows of (firm_id, ok, row_count)."""
    return pl.DataFrame(
        {
            "firm_id": [r[0] for r in rows],
            "platform": ["greenhouse"] * len(rows),
            "ok": [r[1] for r in rows],
            "row_count": [r[2] for r in rows],
            "error": [""] * len(rows),
        },
        schema={
            "firm_id": pl.Utf8,
            "platform": pl.Utf8,
            "ok": pl.Boolean,
            "row_count": pl.Int64,
            "error": pl.Utf8,
        },
    )


def _view(day: date, postings: pl.DataFrame, outcomes: pl.DataFrame, platform: str = "greenhouse"):
    return build_view(day, platform, postings, outcomes)


class TestLifecycle:
    def test_a_new_posting_is_open(self) -> None:
        life = compute_lifecycle([_view(D1, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1)))])
        row = life["gh:a:1"]
        assert row.status is Status.OPEN
        assert row.first_seen == D1 == row.last_seen
        assert row.times_seen == 1

    def test_a_posting_seen_twice_keeps_its_first_seen(self) -> None:
        life = compute_lifecycle(
            [
                _view(D1, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1))),
                _view(D2, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1))),
            ]
        )
        row = life["gh:a:1"]
        assert row.first_seen == D1
        assert row.last_seen == D2
        assert row.times_seen == 2
        assert row.status is Status.OPEN

    def test_disappearing_from_a_healthy_board_closes_it(self) -> None:
        life = compute_lifecycle(
            [
                _view(D1, _postings(("gh:a:1", "a"), ("gh:a:2", "a")), _outcomes(("a", True, 2))),
                _view(D2, _postings(("gh:a:2", "a")), _outcomes(("a", True, 1))),
            ]
        )
        assert life["gh:a:1"].status is Status.CLOSED
        assert life["gh:a:1"].disappeared_on == D2
        assert life["gh:a:2"].status is Status.OPEN

    def test_a_failed_fetch_does_not_close_anything(self) -> None:
        """The whole point of the module.

        One tenant 500s. Its roles are still live; we simply could not look. Marking them
        closed would fire a notification per role and delete a day of real openings from the
        dashboard.
        """
        life = compute_lifecycle(
            [
                _view(D1, _postings(("gh:a:1", "a"), ("gh:a:2", "a")), _outcomes(("a", True, 2))),
                _view(D2, _postings(), _outcomes(("a", False, 0))),
            ]
        )
        for key in ("gh:a:1", "gh:a:2"):
            assert life[key].status is Status.UNKNOWN, f"{key} must not be closed on a failed fetch"
            assert life[key].disappeared_on is None

    def test_a_zero_row_success_also_does_not_close_anything(self) -> None:
        """A tenant with a wrong site name returns 200 and an empty list, not an error.

        Indistinguishable from a real empty board in the data, so it is treated as unknown
        for the same reason.
        """
        life = compute_lifecycle(
            [
                _view(D1, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1))),
                _view(D2, _postings(), _outcomes(("a", True, 0))),
            ]
        )
        assert life["gh:a:1"].status is Status.UNKNOWN

    def test_recovery_after_an_outage_returns_to_open(self) -> None:
        life = compute_lifecycle(
            [
                _view(D1, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1))),
                _view(D2, _postings(), _outcomes(("a", False, 0))),
                _view(D3, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1))),
            ]
        )
        row = life["gh:a:1"]
        assert row.status is Status.OPEN
        assert row.first_seen == D1, "an outage must not restart the clock"

    def test_reappearing_after_a_real_close_is_a_repost(self) -> None:
        life = compute_lifecycle(
            [
                _view(D1, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1))),
                _view(D2, _postings(("gh:a:9", "a")), _outcomes(("a", True, 1))),
                _view(D3, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1))),
            ]
        )
        assert life["gh:a:1"].status is Status.REPOSTED

    def test_one_firms_outage_does_not_close_another_firms_roles(self) -> None:
        life = compute_lifecycle(
            [
                _view(
                    D1,
                    _postings(("gh:a:1", "a"), ("gh:b:1", "b")),
                    _outcomes(("a", True, 1), ("b", True, 1)),
                ),
                _view(D2, _postings(("gh:b:1", "b")), _outcomes(("a", False, 0), ("b", True, 1))),
            ]
        )
        assert life["gh:a:1"].status is Status.UNKNOWN
        assert life["gh:b:1"].status is Status.OPEN

    def test_a_platform_that_did_not_run_leaves_other_platforms_alone(self) -> None:
        """The fast and browser workflows run on different schedules.

        A Greenhouse-only day must not be read as every Workday posting vanishing.
        """
        gh1 = _view(D1, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1)))
        wd1 = build_view(
            D1,
            "workday",
            _postings(("workday:c:1", "c")),
            _outcomes(("c", True, 1)).with_columns(pl.lit("workday").alias("platform")),
        )
        gh2 = _view(D2, _postings(("gh:a:1", "a")), _outcomes(("a", True, 1)))
        life = compute_lifecycle([gh1, wd1, gh2])
        assert life["workday:c:1"].status is Status.OPEN
        assert life["gh:a:1"].status is Status.OPEN


class TestSweepSources:
    """MyCareersFuture is queried by search term, so it writes one outcome for the run."""

    def _sweep(self, day: date, keys: list[str], ok: bool, count: int):
        postings = _postings(*[(k, k.split(":")[1]) for k in keys])
        outcomes = pl.DataFrame(
            {
                "firm_id": ["_mcf_sweep"],
                "platform": ["mcf"],
                "ok": [ok],
                "row_count": [count],
                "error": [""],
            },
            schema={
                "firm_id": pl.Utf8,
                "platform": pl.Utf8,
                "ok": pl.Boolean,
                "row_count": pl.Int64,
                "error": pl.Utf8,
            },
        )
        return build_view(day, "mcf", postings, outcomes)

    def test_a_healthy_sweep_can_close_postings(self) -> None:
        life = compute_lifecycle(
            [
                self._sweep(D1, ["mcf:bytedance:1", "mcf:bytedance:2"], True, 2),
                self._sweep(D2, ["mcf:bytedance:2"], True, 1),
            ]
        )
        assert life["mcf:bytedance:1"].status is Status.CLOSED

    def test_a_failed_sweep_closes_nothing(self) -> None:
        life = compute_lifecycle(
            [
                self._sweep(D1, ["mcf:bytedance:1"], True, 1),
                self._sweep(D2, [], False, 0),
            ]
        )
        assert life["mcf:bytedance:1"].status is Status.UNKNOWN


class TestSnapshotRoundTrip:
    def test_write_and_read_a_snapshot(self, tmp_path) -> None:
        from dataclasses import replace

        from gradtrack.config import load_config

        config = replace(
            load_config(),
            raw_dir=tmp_path / "raw",
        )
        posting = SourcePosting(
            firm_id="janestreet",
            source_platform=Platform.GREENHOUSE,
            external_id="7642974002",
            title="Cybersecurity Detection and Response Analyst",
            apply_url="https://www.janestreet.com/join-jane-street/apply/7642974002",
            location_raw="Singapore",
            posted_date=date(2026, 7, 30),
            posted_date_basis=PostedDateBasis.PUBLISHED,
            extra={"requisition_id": "R-1", "some_number": 3},
        )
        outcome = FetchOutcome("janestreet", "greenhouse", True, 1)
        target = write_snapshot(config, "greenhouse", D1, [posting], [outcome])

        assert (target / POSTINGS_FILE).exists()
        assert (target / OUTCOMES_FILE).exists()
        frame = pl.read_parquet(target / POSTINGS_FILE)
        assert frame["job_key"].to_list() == ["greenhouse:janestreet:7642974002"]
        assert frame["posted_date"].to_list() == [date(2026, 7, 30)]

    def test_an_empty_result_still_writes_its_outcome(self, tmp_path) -> None:
        """An empty partition with no outcomes file looks like a run that never happened."""
        from dataclasses import replace

        from gradtrack.config import load_config

        config = replace(load_config(), raw_dir=tmp_path / "raw")
        target = write_snapshot(
            config, "greenhouse", D1, [], [FetchOutcome("acme", "greenhouse", False, 0, "HTTP 500")]
        )
        outcomes = pl.read_parquet(target / OUTCOMES_FILE)
        assert outcomes["ok"].to_list() == [False]
        assert outcomes["error"].to_list() == ["HTTP 500"]
