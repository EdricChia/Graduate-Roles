"""Greenhouse job board client.

`GET boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`, no auth, no paging — the
whole board comes back in one response.

The property that makes Greenhouse the best source in this project: ``absolute_url`` resolves
to the **firm's own domain**, not to Greenhouse. Verified live — Jane Street returns
`janestreet.com/join-jane-street/apply/{id}` and Coinbase returns
`coinbase.com/careers/positions/{id}`. So the link the dashboard shows is exactly the one the
user asked for, with no extra work.

``content=true`` is not optional. Without it only the title is available, and the classifier
loses every description signal — which for an ATS source, with no structured experience
field, is most of what it has to work with.
"""

from __future__ import annotations

import html
from datetime import datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field

from gradtrack.config import Config
from gradtrack.firms import Firm
from gradtrack.schema import Platform, PostedDateBasis, SourcePosting
from gradtrack.sources.base import FetchOutcome, RateLimiter, contains_singapore, get_json

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


class _Location(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


class _Department(BaseModel):
    model_config = ConfigDict(extra="ignore")
    name: str = ""


class GreenhouseJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str
    absolute_url: str
    location: _Location | None = None
    offices: list[_Location] = Field(default_factory=list)
    departments: list[_Department] = Field(default_factory=list)
    updated_at: datetime | None = None
    first_published: datetime | None = None
    requisition_id: str = ""
    content: str = ""
    education: str = ""


def to_source_posting(job: GreenhouseJob, firm: Firm) -> SourcePosting:
    # first_published is a real publish timestamp; updated_at is a modification time standing
    # in for one. The basis column records which, so the dashboard never implies a precision
    # it does not have.
    if job.first_published:
        posted, basis = job.first_published.date(), PostedDateBasis.PUBLISHED
    elif job.updated_at:
        posted, basis = job.updated_at.date(), PostedDateBasis.UPDATED
    else:
        posted, basis = None, None

    locations = [loc.name for loc in job.offices if loc.name]
    if job.location and job.location.name:
        locations.insert(0, job.location.name)

    return SourcePosting(
        firm_id=firm.firm_id,
        source_platform=Platform.GREENHOUSE,
        external_id=str(job.id),
        title=job.title,
        apply_url=job.absolute_url,
        location_raw="; ".join(dict.fromkeys(locations)),
        # Greenhouse HTML-escapes the body, so it arrives as &lt;p&gt;. Unescaping here means
        # the classifier's tag stripper sees real tags rather than literal entity text.
        description_text=html.unescape(job.content or ""),
        posted_date=posted,
        posted_date_basis=basis,
        department="; ".join(d.name for d in job.departments if d.name),
        extra={
            "requisition_id": job.requisition_id,
            "education": job.education,
            "updated_at": job.updated_at.isoformat() if job.updated_at else "",
        },
    )


def fetch_firm(
    client: httpx.Client, config: Config, firm: Firm, *, singapore_only: bool = True
) -> tuple[list[SourcePosting], FetchOutcome]:
    """Read one firm's Greenhouse board."""
    limiter = RateLimiter(config.rate_limit_for(Platform.GREENHOUSE.value))
    url = BOARD_URL.format(token=firm.ats_token)
    try:
        payload = get_json(client, url, limiter, params={"content": "true"})
    except Exception as exc:  # noqa: BLE001 - recorded so lifecycle can refuse to close
        return [], FetchOutcome(
            firm.firm_id, Platform.GREENHOUSE.value, False, 0, f"{type(exc).__name__}: {exc}"[:200]
        )

    postings: list[SourcePosting] = []
    invalid = 0
    for raw in payload.get("jobs", []) if isinstance(payload, dict) else []:
        try:
            job = GreenhouseJob.model_validate(raw)
        except Exception:  # noqa: BLE001
            invalid += 1
            continue
        posting = to_source_posting(job, firm)
        if singapore_only and not contains_singapore(posting.location_raw):
            continue
        postings.append(posting)

    error = f"{invalid} rows failed validation" if invalid else ""
    return postings, FetchOutcome(
        firm.firm_id, Platform.GREENHOUSE.value, True, len(postings), error
    )
