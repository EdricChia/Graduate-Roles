"""Ashby job board client.

`GET api.ashbyhq.com/posting-api/job-board/{name}`, no auth, no paging.

Ashby is common among quant firms and the better-funded startups, which is exactly the
cohort this tracker cares about. Links stay on `jobs.ashbyhq.com` — the firm's own official
board, not the firm's own domain.

``secondaryLocations`` matters here more than on other platforms: Ashby customers routinely
post one requisition open in several offices, with Singapore listed as a secondary. Reading
only ``location`` drops those.
"""

from __future__ import annotations

from datetime import datetime

import httpx
from pydantic import Field

from gradtrack.config import Config
from gradtrack.firms import Firm
from gradtrack.schema import Platform, PostedDateBasis, SourcePosting
from gradtrack.sources.base import (
    FetchOutcome,
    LenientModel,
    RateLimiter,
    contains_singapore,
    get_json,
)

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{name}"


class _SecondaryLocation(LenientModel):
    location: str = ""


class AshbyJob(LenientModel):
    id: str
    title: str
    location: str = ""
    secondaryLocations: list[_SecondaryLocation] = Field(default_factory=list)
    department: str = ""
    team: str = ""
    employmentType: str = ""
    jobUrl: str = ""
    applyUrl: str = ""
    publishedAt: datetime | None = None
    descriptionPlain: str = ""
    descriptionHtml: str = ""
    isListed: bool = True


def to_source_posting(job: AshbyJob, firm: Firm) -> SourcePosting:
    locations = [job.location, *(s.location for s in job.secondaryLocations)]
    return SourcePosting(
        firm_id=firm.firm_id,
        source_platform=Platform.ASHBY,
        external_id=job.id,
        title=job.title,
        apply_url=job.jobUrl or job.applyUrl,
        location_raw="; ".join(dict.fromkeys(loc for loc in locations if loc)),
        description_text=job.descriptionPlain or job.descriptionHtml,
        posted_date=job.publishedAt.date() if job.publishedAt else None,
        posted_date_basis=PostedDateBasis.PUBLISHED if job.publishedAt else None,
        department="; ".join(x for x in (job.department, job.team) if x),
        employment_type=job.employmentType,
    )


def fetch_firm(
    client: httpx.Client, config: Config, firm: Firm, *, singapore_only: bool = True
) -> tuple[list[SourcePosting], FetchOutcome]:
    limiter = RateLimiter(config.rate_limit_for(Platform.ASHBY.value))
    try:
        payload = get_json(
            client,
            BOARD_URL.format(name=firm.ats_token),
            limiter,
            params={"includeCompensation": "true"},
        )
    except Exception as exc:  # noqa: BLE001
        return [], FetchOutcome(
            firm.firm_id, Platform.ASHBY.value, False, 0, f"{type(exc).__name__}: {exc}"[:200]
        )

    postings, invalid = [], 0
    for raw in payload.get("jobs", []) if isinstance(payload, dict) else []:
        try:
            job = AshbyJob.model_validate(raw)
        except Exception:  # noqa: BLE001
            invalid += 1
            continue
        # Unlisted postings are drafts or private links; they are not open applications.
        if not job.isListed:
            continue
        posting = to_source_posting(job, firm)
        if not posting.apply_url:
            invalid += 1
            continue
        if singapore_only and not contains_singapore(posting.location_raw):
            continue
        postings.append(posting)

    return postings, FetchOutcome(
        firm.firm_id,
        Platform.ASHBY.value,
        True,
        len(postings),
        f"{invalid} rows failed validation" if invalid else "",
    )
