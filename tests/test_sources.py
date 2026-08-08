"""Source payload models.

The null-tolerance tests are the ones that matter. Greenhouse sends
``"requisition_id": null`` on boards that do not use requisition ids, and a field declared
``str = ""`` rejects that outright — so the row fails validation, the client counts it as bad
and moves on, and the firm reports "ok, 0 Singapore postings".

That is indistinguishable from a firm that is not hiring here, and it cost all 104 Jump
Trading postings (17 of them in Singapore), 16 at Akuna and 2 at Point72 in a live run.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from gradtrack.firms import Firm, FirmStatus, RobotsStatus
from gradtrack.schema import Platform
from gradtrack.sources.ashby import AshbyJob
from gradtrack.sources.greenhouse import GreenhouseJob
from gradtrack.sources.greenhouse import to_source_posting as gh_posting
from gradtrack.sources.lever import LeverJob
from gradtrack.sources.lever import to_source_posting as lever_posting
from gradtrack.sources.mcf import McfJob
from gradtrack.sources.workday import WorkdayPosting, parse_posted_on

FIXTURES = Path(__file__).parent / "fixtures"

JANESTREET = Firm(
    firm_id="janestreet",
    firm_name="Jane Street",
    sector="quant",
    tier=1,
    ats_platform=Platform.GREENHOUSE,
    ats_token="janestreet",
    robots_status=RobotsStatus.ALLOWED,
    status=FirmStatus.WIRED,
)


class TestNullTolerance:
    def test_greenhouse_accepts_a_null_requisition_id(self) -> None:
        """The exact payload shape that voided Jump Trading's entire board."""
        job = GreenhouseJob.model_validate(
            {
                "id": 8071050,
                "title": "Accounting Manager | Finance ",
                "absolute_url": "https://www.jumptrading.com/hr/job?gh_jid=8071050",
                "location": {"name": "London"},
                "requisition_id": None,
                "content": None,
                "education": None,
                "first_published": "2026-07-23T10:11:28-04:00",
            }
        )
        assert job.requisition_id == ""
        assert job.content == ""

    def test_lever_accepts_nulls(self) -> None:
        job = LeverJob.model_validate(
            {"id": "abc", "text": "Analyst", "hostedUrl": None, "descriptionPlain": None}
        )
        assert job.hostedUrl == ""

    def test_ashby_accepts_nulls(self) -> None:
        job = AshbyJob.model_validate(
            {"id": "x", "title": "Analyst", "location": None, "department": None}
        )
        assert job.location == ""

    def test_a_genuinely_missing_required_field_still_fails(self) -> None:
        """Null-tolerance must not turn into accepting anything.

        A changed payload shape has to keep failing loudly, which is the whole point of
        validating at the boundary.
        """
        with pytest.raises(Exception):  # noqa: B017 - pydantic ValidationError
            GreenhouseJob.model_validate({"title": "Analyst"})

    def test_an_explicitly_nullable_field_keeps_its_none(self) -> None:
        """`str | None` means the null is meaningful and must not be rewritten to ''."""
        job = GreenhouseJob.model_validate(
            {"id": 1, "title": "A", "absolute_url": "https://x.com/1", "updated_at": None}
        )
        assert job.updated_at is None


class TestGreenhouseMapping:
    def test_real_fixture_maps_cleanly(self) -> None:
        payload = json.loads((FIXTURES / "greenhouse_janestreet.json").read_text(encoding="utf-8"))
        postings = [
            gh_posting(GreenhouseJob.model_validate(raw), JANESTREET) for raw in payload["jobs"]
        ]
        assert postings
        for posting in postings:
            assert posting.apply_url.startswith("https://")
            assert posting.job_key.startswith("greenhouse:janestreet:")

    def test_the_apply_url_is_the_firms_own_domain(self) -> None:
        """The property that makes Greenhouse the best source here."""
        payload = json.loads((FIXTURES / "greenhouse_janestreet.json").read_text(encoding="utf-8"))
        urls = [
            gh_posting(GreenhouseJob.model_validate(raw), JANESTREET).apply_url
            for raw in payload["jobs"]
        ]
        assert all("janestreet.com" in url for url in urls)

    def test_first_published_beats_updated_at(self) -> None:
        job = GreenhouseJob.model_validate(
            {
                "id": 1,
                "title": "A",
                "absolute_url": "https://x.com/1",
                "first_published": "2026-07-23T10:11:28-04:00",
                "updated_at": "2026-08-01T10:11:28-04:00",
            }
        )
        posting = gh_posting(job, JANESTREET)
        assert posting.posted_date == date(2026, 7, 23)
        assert posting.posted_date_basis is not None
        assert posting.posted_date_basis.value == "published"


class TestLeverDates:
    def test_created_at_is_milliseconds_not_seconds(self) -> None:
        """Dividing by the wrong factor silently files every posting under 1970."""
        job = LeverJob.model_validate(
            {
                "id": "a",
                "text": "Analyst",
                "hostedUrl": "https://jobs.lever.co/x/a",
                "createdAt": 1754006400000,
            }
        )
        posting = lever_posting(job, JANESTREET)
        assert posting.posted_date is not None
        assert posting.posted_date.year == 2025


class TestWorkdayDates:
    TODAY = date(2026, 8, 8)

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Posted Today", date(2026, 8, 8)),
            ("Posted Yesterday", date(2026, 8, 7)),
            ("Posted 3 Days Ago", date(2026, 8, 5)),
        ],
    )
    def test_relative_dates_resolve(self, text: str, expected: date) -> None:
        assert parse_posted_on(text, self.TODAY)[0] == expected

    def test_thirty_plus_days_is_left_null_rather_than_invented(self) -> None:
        """ "30+" means at least thirty. Recording exactly thirty is a fabricated value in a
        column the dashboard sorts by."""
        assert parse_posted_on("Posted 30+ Days Ago", self.TODAY)[0] is None

    def test_an_unparseable_string_is_null(self) -> None:
        assert parse_posted_on("", self.TODAY)[0] is None

    def test_the_list_payload_validates(self) -> None:
        posting = WorkdayPosting.model_validate(
            {
                "title": "Graduate Software Engineer",
                "externalPath": "/job/Singapore/Grad-SWE_R-1234",
                "locationsText": "2 Locations",
                "postedOn": "Posted 3 Days Ago",
                "bulletFields": ["R-1234"],
            }
        )
        assert posting.bulletFields == ["R-1234"]


class TestMcfDates:
    def test_original_posting_date_is_used_not_the_repost_date(self) -> None:
        """A role created in May and reposted in July is not a July role."""
        job = McfJob.model_validate(
            {
                "uuid": "u1",
                "title": "Management Associate",
                "metadata": {
                    "jobPostId": "MCF-2026-1",
                    "jobDetailsUrl": "https://www.mycareersfuture.gov.sg/job/x",
                    "originalPostingDate": "2026-05-13",
                    "newPostingDate": "2026-07-14",
                    "expiryDate": "2026-08-13",
                },
            }
        )
        from gradtrack.sources.mcf import to_source_posting

        posting = to_source_posting(job)
        assert posting.posted_date == date(2026, 5, 13)

    def test_the_hiring_company_wins_over_the_posting_agency(self) -> None:
        job = McfJob.model_validate(
            {
                "uuid": "u1",
                "title": "Analyst",
                "postedCompany": {"name": "TOTAL MANPOWER PTE. LTD.", "uen": "1"},
                "hiringCompany": {"name": "ASML SINGAPORE PTE LTD", "uen": "2"},
                "metadata": {
                    "jobPostId": "MCF-2026-2",
                    "jobDetailsUrl": "https://www.mycareersfuture.gov.sg/job/y",
                    "originalPostingDate": "2026-08-01",
                },
            }
        )
        assert job.employer.name == "ASML SINGAPORE PTE LTD"
