"""SmartRecruiters postings client.

`GET api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset=N`, no auth,
offset paged.

Two things make this client shaped differently from the others.

The list response carries no description — that needs a second request per posting. So the
Singapore filter is applied to the list first, using the structured ``location.country``
field, and details are fetched only for the survivors. A large employer might list 900 roles
worldwide and eight in Singapore; fetching 900 descriptions to discard 892 is the difference
between a fast run and a rate-limited one.

The list response does carry ``experienceLevel.label`` — values like "Entry Level",
"Associate", "Mid-Senior Level". That is a genuinely useful structured seniority signal, and
one of the very few any ATS provides, so it is passed through to the classifier in ``extra``.
"""

from __future__ import annotations

from datetime import datetime

import httpx

from gradtrack.config import Config
from gradtrack.firms import Firm
from gradtrack.schema import Platform, PostedDateBasis, SourcePosting
from gradtrack.sources.base import FetchOutcome, LenientModel, RateLimiter, get_json

POSTINGS_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"
DETAIL_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting}"
PUBLIC_URL = "https://jobs.smartrecruiters.com/{company}/{posting}"
PAGE_SIZE = 100
MAX_PAGES = 20


class _Label(LenientModel):
    label: str = ""


class _Location(LenientModel):
    city: str = ""
    region: str = ""
    country: str = ""
    remote: bool = False


class SmartRecruitersPosting(LenientModel):
    id: str
    name: str
    uuid: str = ""
    refNumber: str = ""
    releasedDate: datetime | None = None
    location: _Location | None = None
    department: _Label | None = None
    function: _Label | None = None
    typeOfEmployment: _Label | None = None
    experienceLevel: _Label | None = None


def _is_singapore(posting: SmartRecruitersPosting) -> bool:
    loc = posting.location or _Location()
    return loc.country.lower() in {"sg", "sgp", "singapore"} or "singapore" in loc.city.lower()


def _extract_description(detail: object) -> str:
    """Pull the prose out of a posting detail payload.

    The body lives under ``jobAd.sections.{companyDescription,jobDescription,qualifications,
    additionalInformation}.text``. Qualifications is the section that actually carries the
    experience requirement, so all of them are concatenated rather than just the first.
    """
    if not isinstance(detail, dict):
        return ""
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    if not isinstance(sections, dict):
        return ""
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
        section = sections.get(key)
        if isinstance(section, dict) and section.get("text"):
            parts.append(str(section["text"]))
    return "\n".join(parts)


def to_source_posting(
    posting: SmartRecruitersPosting, firm: Firm, description: str
) -> SourcePosting:
    loc = posting.location or _Location()
    return SourcePosting(
        firm_id=firm.firm_id,
        source_platform=Platform.SMARTRECRUITERS,
        external_id=posting.id,
        title=posting.name,
        apply_url=PUBLIC_URL.format(company=firm.ats_token, posting=posting.id),
        location_raw="; ".join(x for x in (loc.city, loc.region, loc.country) if x),
        description_text=description,
        posted_date=posting.releasedDate.date() if posting.releasedDate else None,
        posted_date_basis=PostedDateBasis.PUBLISHED if posting.releasedDate else None,
        department=(posting.department.label if posting.department else ""),
        employment_type=(posting.typeOfEmployment.label if posting.typeOfEmployment else ""),
        extra={
            "ref_number": posting.refNumber,
            "experience_level": (posting.experienceLevel.label if posting.experienceLevel else ""),
            "function": posting.function.label if posting.function else "",
            "remote": loc.remote,
        },
    )


def fetch_firm(
    client: httpx.Client, config: Config, firm: Firm, *, singapore_only: bool = True
) -> tuple[list[SourcePosting], FetchOutcome]:
    limiter = RateLimiter(config.rate_limit_for(Platform.SMARTRECRUITERS.value))
    company = firm.ats_token
    candidates: list[SmartRecruitersPosting] = []
    invalid = 0

    for page in range(MAX_PAGES):
        try:
            payload = get_json(
                client,
                POSTINGS_URL.format(company=company),
                limiter,
                params={"limit": PAGE_SIZE, "offset": page * PAGE_SIZE},
            )
        except Exception as exc:  # noqa: BLE001
            return [], FetchOutcome(
                firm.firm_id,
                Platform.SMARTRECRUITERS.value,
                False,
                0,
                f"{type(exc).__name__}: {exc}"[:200],
            )
        content = payload.get("content", []) if isinstance(payload, dict) else []
        for raw in content:
            try:
                posting = SmartRecruitersPosting.model_validate(raw)
            except Exception:  # noqa: BLE001
                invalid += 1
                continue
            if singapore_only and not _is_singapore(posting):
                continue
            candidates.append(posting)
        if len(content) < PAGE_SIZE:
            break

    postings = []
    for posting in candidates:
        description = ""
        try:
            detail = get_json(
                client, DETAIL_URL.format(company=company, posting=posting.id), limiter
            )
            description = _extract_description(detail)
        except Exception:  # noqa: BLE001
            # A missing description costs classification accuracy on this row but is not a
            # reason to drop it or to fail the firm.
            pass
        postings.append(to_source_posting(posting, firm, description))

    return postings, FetchOutcome(
        firm.firm_id,
        Platform.SMARTRECRUITERS.value,
        True,
        len(postings),
        f"{invalid} rows failed validation" if invalid else "",
    )
