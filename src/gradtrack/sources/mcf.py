"""MyCareersFuture client.

A government-operated public API with no key and a `robots.txt` of `User-agent: *` /
`Disallow:` — everything permitted.

**This is a secondary source and the code should be read with that in mind.** Firms publish
to their own ATS first and syndicate here days later, because posting is driven by Fair
Consideration Framework compliance timing rather than recruiting urgency. So MyCareersFuture
is used for three things — discovering firms not yet in the registry, cross-checking the ATS
legs, and enriching rows with the salary band that ATS feeds never carry — and never for the
apply link when an ATS row exists for the same role.

Two traps, both verified against live data, both handled here:

* ``expiryDate`` is a posting TTL, not an application deadline. Sampled: 07-30 to 08-30,
  07-24 to 08-23, 08-03 to 09-02 — always about thirty days. It is deliberately not mapped
  to anything the dashboard can show as a deadline.
* ``newPostingDate`` is the *repost* date. A role created 2026-05-13 and reposted 2026-07-14
  carries ``newPostingDate: 2026-07-14``. ``originalPostingDate`` is the one that means what
  a reader assumes, and using the wrong one reports months-old listings as fresh.
"""

from __future__ import annotations

from datetime import date, datetime

import httpx
from pydantic import Field

from gradtrack.config import Config
from gradtrack.schema import Platform, PostedDateBasis, SourcePosting
from gradtrack.sources.base import FetchOutcome, LenientModel, RateLimiter, get_json

SEARCH_URL = "https://api.mycareersfuture.gov.sg/v2/jobs"
PAGE_SIZE = 100
# The API accepts deep paging, but every term saturates well before this and the tail is
# increasingly off-topic. Bounded so a bad term cannot turn into a thousand requests.
MAX_PAGES_PER_TERM = 4

# MyCareersFuture has no "graduate roles" filter, so coverage comes from a term sweep. The
# list spans the two things we care about: how graduate roles describe themselves, and the
# job families in the taxonomy. Terms are cheap — each is at most four requests — and the
# union is deduplicated by uuid, so over-covering costs little and under-covering is
# invisible.
SEARCH_TERMS: tuple[str, ...] = (
    # Graduate-shaped
    "graduate",
    "graduate programme",
    "graduate program",
    "management associate",
    "management trainee",
    "fresh graduate",
    "entry level",
    "trainee",
    "campus",
    "early career",
    # Families
    "analyst",
    "associate",
    "software engineer",
    "data analyst",
    "data scientist",
    "business analyst",
    "quantitative",
    "consultant",
    "strategy",
    "supply chain",
    "operations",
    "product manager",
    "investment",
    "risk",
    "audit",
    "finance",
    "marketing",
    "human resources",
    "engineer",
)


class _SalaryType(LenientModel):
    salaryType: str = ""


class _Salary(LenientModel):
    minimum: float | None = None
    maximum: float | None = None
    type: _SalaryType | None = None


class _Company(LenientModel):
    name: str = ""
    uen: str = ""


class _Named(LenientModel):
    """positionLevels / employmentTypes / categories all share this shape with a different key."""

    position: str = ""
    employmentType: str = ""
    category: str = ""


class _Metadata(LenientModel):
    # LenientModel ignores unknown keys, and the metadata block carries a couple of dozen
    # operational fields we have no use for. The ones we depend on are required below, so a
    # removal still fails loudly — which is the property the ingest rule actually asks for.
    jobPostId: str
    jobDetailsUrl: str
    originalPostingDate: date
    newPostingDate: date | None = None
    expiryDate: date | None = None
    createdAt: datetime | None = None
    updatedAt: datetime | None = None
    repostCount: int = 0
    isPostedOnBehalf: bool = False
    totalNumberOfView: int = 0
    totalNumberJobApplication: int = 0


class McfJob(LenientModel):
    """One MyCareersFuture posting, validated at the ingest boundary."""

    uuid: str
    title: str
    description: str = ""
    minimumYearsExperience: int | None = None
    positionLevels: list[_Named] = Field(default_factory=list)
    employmentTypes: list[_Named] = Field(default_factory=list)
    categories: list[_Named] = Field(default_factory=list)
    postedCompany: _Company | None = None
    hiringCompany: _Company | None = None
    salary: _Salary | None = None
    metadata: _Metadata

    @property
    def employer(self) -> _Company:
        """The firm actually hiring.

        When an agency posts on behalf of a client, ``hiringCompany`` names the client and
        ``postedCompany`` names the agency. Preferring the client is what lets an agency
        repost of a real MNC role still match the registry.
        """
        if self.hiringCompany and self.hiringCompany.name.strip():
            return self.hiringCompany
        return self.postedCompany or _Company()


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in value.strip().lower()).strip("-")


def to_source_posting(job: McfJob) -> SourcePosting:
    """Map a validated MyCareersFuture job onto the unified schema."""
    employer = job.employer
    salary = job.salary or _Salary()
    return SourcePosting(
        # Unresolved at ingest time. transform/dedupe.py maps this onto a registry firm_id
        # via UEN or the alias table; unmatched rows become discovery candidates.
        firm_id=_slug(employer.name) or (f"uen-{employer.uen}" if employer.uen else "unknown"),
        source_platform=Platform.MCF,
        external_id=job.uuid,
        title=job.title,
        apply_url=job.metadata.jobDetailsUrl,
        location_raw="Singapore",
        description_text=job.description,
        # originalPostingDate, never newPostingDate — see the module docstring.
        posted_date=job.metadata.originalPostingDate,
        posted_date_basis=PostedDateBasis.PUBLISHED,
        department="; ".join(c.category for c in job.categories if c.category),
        employment_type="; ".join(
            e.employmentType for e in job.employmentTypes if e.employmentType
        ),
        extra={
            "mcf_job_post_id": job.metadata.jobPostId,
            "uen": employer.uen,
            "employer_name": employer.name,
            "posted_company_name": (job.postedCompany.name if job.postedCompany else ""),
            "is_posted_on_behalf": job.metadata.isPostedOnBehalf,
            "min_years": job.minimumYearsExperience,
            "position_levels": "|".join(p.position for p in job.positionLevels if p.position),
            "employment_types": "|".join(
                e.employmentType for e in job.employmentTypes if e.employmentType
            ),
            "salary_min": salary.minimum,
            "salary_max": salary.maximum,
            "salary_type": salary.type.salaryType if salary.type else "",
            "repost_count": job.metadata.repostCount,
            # Kept because it is genuinely informative about a posting's shelf life, and
            # named so nobody mistakes it for a deadline.
            "posting_expires_on": (
                job.metadata.expiryDate.isoformat() if job.metadata.expiryDate else ""
            ),
            "views": job.metadata.totalNumberOfView,
            "applications": job.metadata.totalNumberJobApplication,
        },
    )


def fetch_all(
    client: httpx.Client,
    config: Config,
    *,
    terms: tuple[str, ...] = SEARCH_TERMS,
    max_pages: int = MAX_PAGES_PER_TERM,
) -> tuple[list[SourcePosting], FetchOutcome]:
    """Sweep the search terms and return the deduplicated union.

    A term that fails is logged into the outcome's error string but does not abort the
    sweep: losing one term costs some coverage, losing the whole run costs a day.
    """
    limiter = RateLimiter(config.rate_limit_for("mcf"))
    seen: dict[str, SourcePosting] = {}
    errors: list[str] = []

    for term in terms:
        for page in range(max_pages):
            try:
                payload = get_json(
                    client,
                    SEARCH_URL,
                    limiter,
                    params={"search": term, "limit": PAGE_SIZE, "page": page},
                )
            except Exception as exc:  # noqa: BLE001 - recorded, sweep continues
                errors.append(f"{term} p{page}: {type(exc).__name__}")
                break

            results = payload.get("results", []) if isinstance(payload, dict) else []
            for raw in results:
                try:
                    job = McfJob.model_validate(raw)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{term} p{page}: validation {type(exc).__name__}")
                    continue
                if job.uuid not in seen:
                    seen[job.uuid] = to_source_posting(job)

            if len(results) < PAGE_SIZE:
                break

    postings = list(seen.values())
    # ok is false only if the sweep produced nothing at all. Partial failure across a few
    # terms is normal and must not mark the whole source unusable, or one flaky term would
    # freeze every MyCareersFuture posting at its previous status.
    outcome = FetchOutcome(
        firm_id="_mcf_sweep",
        platform=Platform.MCF.value,
        ok=bool(postings),
        row_count=len(postings),
        error="; ".join(errors[:5]),
    )
    return postings, outcome
