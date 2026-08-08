"""SuccessFactors HTML parsing, against a real captured page.

This is the one source parsed out of HTML rather than JSON, so it is the one that breaks
when a vendor changes a template. The fixture is three real tiles from
`jobs.temasek.com.sg`, captured 2026-08-08.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gradtrack.firms import Firm, FirmStatus, RobotsStatus
from gradtrack.schema import Platform
from gradtrack.sources.successfactors import is_singapore, parse_tiles

FIXTURE = Path(__file__).parent / "fixtures" / "successfactors_temasek.html"

TEMASEK = Firm(
    firm_id="temasek",
    firm_name="Temasek",
    sector="sovereign",
    tier=1,
    ats_platform=Platform.SUCCESSFACTORS,
    ats_host="jobs.temasek.com.sg",
    robots_status=RobotsStatus.ALLOWED,
    status=FirmStatus.WIRED,
)


@pytest.fixture(scope="module")
def postings():
    return parse_tiles(FIXTURE.read_text(encoding="utf-8"), "https://jobs.temasek.com.sg", TEMASEK)


class TestParsing:
    def test_every_tile_is_extracted(self, postings) -> None:
        assert len(postings) == 3

    def test_titles_are_clean_text(self, postings) -> None:
        for posting in postings:
            assert posting.title.strip() == posting.title
            assert "<" not in posting.title
            assert posting.title

    def test_apply_url_is_on_the_firms_own_domain(self, postings) -> None:
        """The whole point. SuccessFactors is one of only two platforms that manage this."""
        for posting in postings:
            assert posting.apply_url.startswith("https://jobs.temasek.com.sg/job/")

    def test_job_keys_are_unique_and_well_formed(self, postings) -> None:
        keys = [p.job_key for p in postings]
        assert len(set(keys)) == len(keys)
        for key in keys:
            assert key.startswith("successfactors:temasek:")

    def test_location_is_captured_without_its_label(self, postings) -> None:
        """The template repeats each field name in a screen-reader span.

        Left in, every location reads "Location Location SG, 238891".
        """
        locations = [p.location_raw for p in postings]
        assert any(locations), "no location parsed at all"
        for location in locations:
            assert not location.lower().startswith("location")

    def test_dates_are_marked_observed_not_invented(self, postings) -> None:
        """Tiles carry no publish date, and the basis column has to say so."""
        for posting in postings:
            assert posting.posted_date is None
            assert posting.posted_date_basis is not None
            assert posting.posted_date_basis.value == "observed"


class TestSingaporeDetection:
    @pytest.mark.parametrize("location", ["SG, 238891", "Singapore", "sg", "SGP", "SG, Singapore"])
    def test_accepts(self, location: str) -> None:
        assert is_singapore(location)

    @pytest.mark.parametrize("location", ["US, 10001", "HK, Central", "London", "", "Glasgow"])
    def test_rejects(self, location: str) -> None:
        """'Glasgow' contains "sg" and must not match — the pattern is word-bounded."""
        assert not is_singapore(location)
