"""Lever postings client.

`GET api.lever.co/v0/postings/{slug}?mode=json`, no auth, documented at
github.com/lever/postings-api. Returns a flat array — no paging.

Unlike Greenhouse, Lever's links stay on `jobs.lever.co/{slug}/{id}`. That is still the
firm's own official board rather than an aggregator listing, so it satisfies the "apply on
the company's site, not a job board" requirement — but it is worth knowing the URL is not on
the company's own domain. `SOURCES.md` records which platforms are which.

Two limitations from the docs, both recorded rather than worked around: many Lever customers
disable the public endpoint, and there is no endpoint that enumerates slugs, so every slug
has to be discovered and written into the registry by hand.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
from pydantic import BaseModel, ConfigDict, Field

from gradtrack.config import Config
from gradtrack.firms import Firm
from gradtrack.schema import Platform, PostedDateBasis, SourcePosting
from gradtrack.sources.base import FetchOutcome, RateLimiter, contains_singapore, get_json

POSTINGS_URL = "https://api.lever.co/v0/postings/{slug}"


class _Categories(BaseModel):
    model_config = ConfigDict(extra="ignore")
    commitment: str = ""
    department: str = ""
    location: str = ""
    team: str = ""


class _ListItem(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str = ""
    content: str = ""


class LeverJob(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    text: str
    hostedUrl: str = ""
    applyUrl: str = ""
    createdAt: int | None = None
    categories: _Categories | None = None
    description: str = ""
    descriptionPlain: str = ""
    additionalPlain: str = ""
    lists: list[_ListItem] = Field(default_factory=list)
    workplaceType: str = ""


def to_source_posting(job: LeverJob, firm: Firm) -> SourcePosting:
    cats = job.categories or _Categories()
    posted = None
    if job.createdAt:
        # Milliseconds since epoch, not seconds. Dividing by the wrong factor silently puts
        # every posting in 1970 and makes the "newest first" default view useless.
        posted = datetime.fromtimestamp(job.createdAt / 1000, tz=UTC).date()

    # The requirements list is where "final year" and "0-2 years" usually live, so it is
    # folded into the description the classifier reads.
    body = "\n".join(
        [
            job.descriptionPlain or job.description,
            *(item.content for item in job.lists),
            job.additionalPlain,
        ]
    ).strip()

    return SourcePosting(
        firm_id=firm.firm_id,
        source_platform=Platform.LEVER,
        external_id=job.id,
        title=job.text,
        apply_url=job.hostedUrl or job.applyUrl,
        location_raw=cats.location,
        description_text=body,
        posted_date=posted,
        posted_date_basis=PostedDateBasis.PUBLISHED if posted else None,
        department="; ".join(x for x in (cats.department, cats.team) if x),
        employment_type=cats.commitment,
        extra={"workplace_type": job.workplaceType},
    )


def fetch_firm(
    client: httpx.Client, config: Config, firm: Firm, *, singapore_only: bool = True
) -> tuple[list[SourcePosting], FetchOutcome]:
    limiter = RateLimiter(config.rate_limit_for(Platform.LEVER.value))
    try:
        payload = get_json(
            client, POSTINGS_URL.format(slug=firm.ats_token), limiter, params={"mode": "json"}
        )
    except Exception as exc:  # noqa: BLE001
        return [], FetchOutcome(
            firm.firm_id, Platform.LEVER.value, False, 0, f"{type(exc).__name__}: {exc}"[:200]
        )

    postings, invalid = [], 0
    for raw in payload if isinstance(payload, list) else []:
        try:
            job = LeverJob.model_validate(raw)
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
        Platform.LEVER.value,
        True,
        len(postings),
        f"{invalid} rows failed validation" if invalid else "",
    )
