"""Schema invariants. These guard the two things that break quietly rather than loudly."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from gradtrack.schema import (
    FAMILY_GROUPS,
    PRIORITY_GROUPS,
    JobFamily,
    Platform,
    PostedDateBasis,
    SourcePosting,
    make_job_key,
)


class TestJobKey:
    """The join key for every lifecycle comparison. Instability here means every posting
    looks new every day, and the notifier fires the whole board at you each morning."""

    def test_it_is_stable_across_incidental_formatting(self) -> None:
        assert make_job_key("greenhouse", "janestreet", "7642974002") == make_job_key(
            "GREENHOUSE", " JaneStreet ", "7642974002"
        )

    def test_unsafe_characters_collapse_rather_than_leak_into_the_key(self) -> None:
        assert make_job_key("workday", "gic", "R-044 12/ext") == "workday:gic:r-044-12-ext"

    @pytest.mark.parametrize(
        ("platform", "firm_id", "external_id"),
        [("greenhouse", "janestreet", ""), ("", "janestreet", "1"), ("greenhouse", "  ", "1")],
    )
    def test_a_blank_component_is_refused(
        self, platform: str, firm_id: str, external_id: str
    ) -> None:
        with pytest.raises(ValueError, match="three non-empty parts"):
            make_job_key(platform, firm_id, external_id)


class TestSourcePosting:
    def test_a_well_formed_posting_validates(self) -> None:
        posting = SourcePosting(
            firm_id="janestreet",
            source_platform=Platform.GREENHOUSE,
            external_id="7642974002",
            title="Cybersecurity Detection and Response Analyst",
            apply_url="https://www.janestreet.com/join-jane-street/apply/7642974002",
            location_raw="Singapore",
            posted_date=date(2026, 7, 30),
            posted_date_basis=PostedDateBasis.PUBLISHED,
        )
        assert posting.job_key == "greenhouse:janestreet:7642974002"

    def test_a_relative_apply_url_is_refused(self) -> None:
        """The link is the deliverable. A relative path silently 404s from the dashboard."""
        with pytest.raises(ValidationError, match="absolute http"):
            SourcePosting(
                firm_id="janestreet",
                source_platform=Platform.GREENHOUSE,
                external_id="1",
                title="Analyst",
                apply_url="/join-jane-street/apply/1",
            )

    def test_an_unexpected_field_is_refused(self) -> None:
        """extra='forbid' is what turns an upstream payload change into a loud failure."""
        with pytest.raises(ValidationError):
            SourcePosting(
                firm_id="janestreet",
                source_platform=Platform.GREENHOUSE,
                external_id="1",
                title="Analyst",
                apply_url="https://example.com/1",
                closing_date="2026-09-30",
            )

    def test_a_blank_title_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            SourcePosting(
                firm_id="janestreet",
                source_platform=Platform.GREENHOUSE,
                external_id="1",
                title="   ",
                apply_url="https://example.com/1",
            )


class TestTaxonomy:
    def test_every_family_has_a_group(self) -> None:
        """A family added without a group would vanish from the alert filter unnoticed."""
        assert set(FAMILY_GROUPS) == set(JobFamily)

    def test_the_six_priority_groups_all_exist(self) -> None:
        assert set(FAMILY_GROUPS.values()) >= PRIORITY_GROUPS
