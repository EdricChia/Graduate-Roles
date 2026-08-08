"""Workable job board client.

`GET apply.workable.com/api/v1/widget/accounts/{account}?details=true`, no auth.

``details=true`` returns description, requirements and benefits inline, which avoids the
per-posting second request SmartRecruiters needs.

Workable exposes two structured fields no other platform here does: ``experience`` and
``education``, with values like "Entry level" and "Bachelor's Degree". Both are passed to the
classifier, which is worth noting because ATS sources otherwise have no structured seniority
signal at all and fall back entirely on prose.
"""

from __future__ import annotations

from datetime import date

import httpx

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

ACCOUNT_URL = "https://apply.workable.com/api/v1/widget/accounts/{account}"


class WorkableJob(LenientModel):
    title: str
    shortcode: str
    code: str = ""
    url: str = ""
    shortlink: str = ""
    application_url: str = ""
    employment_type: str = ""
    department: str = ""
    function: str = ""
    country: str = ""
    city: str = ""
    state: str = ""
    telecommuting: bool = False
    published_on: date | None = None
    created_at: date | None = None
    description: str = ""
    requirements: str = ""
    benefits: str = ""
    education: str = ""
    experience: str = ""


def to_source_posting(job: WorkableJob, firm: Firm) -> SourcePosting:
    posted = job.published_on or job.created_at
    return SourcePosting(
        firm_id=firm.firm_id,
        source_platform=Platform.WORKABLE,
        external_id=job.shortcode,
        title=job.title,
        apply_url=job.url or job.shortlink or job.application_url,
        location_raw="; ".join(x for x in (job.city, job.state, job.country) if x),
        # Requirements is where the experience demand lives; dropping it would blind the
        # years-veto on this platform.
        description_text="\n".join(x for x in (job.description, job.requirements) if x),
        posted_date=posted,
        posted_date_basis=PostedDateBasis.PUBLISHED if posted else None,
        department="; ".join(x for x in (job.department, job.function) if x),
        employment_type=job.employment_type,
        extra={
            "code": job.code,
            "education": job.education,
            "experience": job.experience,
            "telecommuting": job.telecommuting,
        },
    )


def fetch_firm(
    client: httpx.Client, config: Config, firm: Firm, *, singapore_only: bool = True
) -> tuple[list[SourcePosting], FetchOutcome]:
    limiter = RateLimiter(config.rate_limit_for(Platform.WORKABLE.value))
    try:
        payload = get_json(
            client,
            ACCOUNT_URL.format(account=firm.ats_token),
            limiter,
            params={"details": "true"},
        )
    except Exception as exc:  # noqa: BLE001
        return [], FetchOutcome(
            firm.firm_id, Platform.WORKABLE.value, False, 0, f"{type(exc).__name__}: {exc}"[:200]
        )

    postings, invalid = [], 0
    for raw in payload.get("jobs", []) if isinstance(payload, dict) else []:
        try:
            job = WorkableJob.model_validate(raw)
        except Exception:  # noqa: BLE001
            invalid += 1
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
        Platform.WORKABLE.value,
        True,
        len(postings),
        f"{invalid} rows failed validation" if invalid else "",
    )
