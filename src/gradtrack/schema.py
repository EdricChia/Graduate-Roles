"""The unified posting schema — this project's data contract, expressed in code.

Every ATS speaks a different dialect. A source client's job is to return
:class:`SourcePosting`, and nothing downstream is allowed to care which platform it came
from. The columns that get *derived* later (family, grad eligibility, lifecycle status) are
deliberately not on :class:`SourcePosting`: ingest never classifies, and never decides
whether a role is still open.

Three invariants are enforced here rather than by convention:

* ``apply_url`` must be an absolute URL. It is the thing the user actually clicks, and the
  whole point of reading career sites directly is that it lands on the firm's own domain.
* ``job_key`` is built from ``(platform, firm_id, external_id)`` and must be stable across
  snapshots. Lifecycle diffing is a set comparison on this key; an unstable key silently
  reports every role as new every day.
* ``posted_date_basis`` travels with ``posted_date``. A date we observed is not a date the
  firm published, and the UI has to be able to say which it is showing.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Platform(StrEnum):
    """ATS platforms we read. Values match the registry's ``ats_platform`` column."""

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    WORKABLE = "workable"
    WORKDAY = "workday"
    SUCCESSFACTORS = "successfactors"
    ORACLE_ORC = "oracle_orc"
    EIGHTFOLD = "eightfold"
    PHENOM = "phenom"
    BROWSER = "browser"
    MCF = "mcf"


class Status(StrEnum):
    """Lifecycle state of a posting, derived by diffing consecutive snapshots.

    ``UNKNOWN`` is the load-bearing one. A posting absent from today's snapshot is only
    ``CLOSED`` if we successfully read that firm's board and it genuinely was not there. If
    the fetch failed, or returned zero rows, the posting is ``UNKNOWN`` and keeps its prior
    state. Without that distinction one Workday tenant returning a 500 marks eighty live
    roles as closed and fires eighty notifications.
    """

    OPEN = "open"
    CLOSED = "closed"
    REPOSTED = "reposted"
    UNKNOWN = "unknown"


class JobFamily(StrEnum):
    """Role taxonomy. Assigned by an ordered rule list in ``transform/classify.py``."""

    STRATEGY_CONSULTING = "strategy_consulting"
    MANAGEMENT_CONSULTING = "management_consulting"
    STRATEGY_OPERATIONS = "strategy_operations"
    OPERATIONS = "operations"
    SUPPLY_CHAIN = "supply_chain"
    SOFTWARE_ENGINEERING = "software_engineering"
    DATA_SCIENCE = "data_science"
    DATA_ANALYST = "data_analyst"
    BUSINESS_ANALYST = "business_analyst"
    QUANT_TRADING = "quant_trading"
    QUANT_RESEARCH = "quant_research"
    QUANT_DEV = "quant_dev"
    INVESTMENT = "investment"
    PRODUCT_MANAGEMENT = "product_management"
    RISK_COMPLIANCE = "risk_compliance"
    FINANCE_ACCOUNTING = "finance_accounting"
    ENGINEERING = "engineering"
    SALES_MARKETING = "sales_marketing"
    HUMAN_RESOURCES = "human_resources"
    LEGAL = "legal"
    # Rotational graduate programmes that name no discipline at all — "Graduate Management
    # Associate", "GOglobal Graduate Program". A real category, not a dumping ground: 345 of
    # 662 graduate rows in the first pool landed in OTHER, and most of them were these.
    GENERAL_MANAGEMENT = "general_management"
    OTHER = "other"


# Families collapse into the six groups the user picked for Telegram alerts. Keeping the
# fine-grained family on the row and the group as a lookup means the alert filter can widen
# or narrow without a reclassification pass over the whole table.
FAMILY_GROUPS: dict[JobFamily, str] = {
    JobFamily.STRATEGY_CONSULTING: "Strategy & Consulting",
    JobFamily.MANAGEMENT_CONSULTING: "Strategy & Consulting",
    JobFamily.STRATEGY_OPERATIONS: "Strategy & Consulting",
    JobFamily.QUANT_TRADING: "Quant & Trading",
    JobFamily.QUANT_RESEARCH: "Quant & Trading",
    JobFamily.QUANT_DEV: "Quant & Trading",
    JobFamily.DATA_SCIENCE: "Data & Analytics",
    JobFamily.DATA_ANALYST: "Data & Analytics",
    JobFamily.BUSINESS_ANALYST: "Data & Analytics",
    JobFamily.SOFTWARE_ENGINEERING: "SWE & Technical",
    JobFamily.OPERATIONS: "Operations",
    JobFamily.SUPPLY_CHAIN: "Supply Chain",
    JobFamily.INVESTMENT: "Investment",
    JobFamily.PRODUCT_MANAGEMENT: "Product",
    JobFamily.RISK_COMPLIANCE: "Risk & Compliance",
    JobFamily.FINANCE_ACCOUNTING: "Finance",
    JobFamily.ENGINEERING: "Engineering",
    JobFamily.SALES_MARKETING: "Sales & Marketing",
    JobFamily.HUMAN_RESOURCES: "People",
    JobFamily.LEGAL: "Legal",
    JobFamily.GENERAL_MANAGEMENT: "General Management",
    JobFamily.OTHER: "Other",
}

# The six the user asked to be pushed to their phone. Everything else is still tracked and
# still visible in the dashboard; this only gates notifications.
PRIORITY_GROUPS: frozenset[str] = frozenset(
    {
        "Strategy & Consulting",
        "Quant & Trading",
        "Data & Analytics",
        "SWE & Technical",
        "Operations",
        "Supply Chain",
    }
)


class RoleType(StrEnum):
    """The three legs of scope, as a value a user can filter on.

    Derived from the classifier's ``grad_basis`` rather than recomputed: the route that
    admitted a posting already says which leg it came in on, and deriving it twice would let
    the two drift.
    """

    PROGRAMME = "Graduate programme"
    GRADUATE_ROLE = "Graduate role"
    ENTRY_LEVEL = "Entry level"
    INTERNSHIP = "Internship"
    NOT_GRADUATE = "Not graduate"


# grad_basis -> role type. Kept beside the enum so a new route cannot be added without
# deciding which leg it belongs to.
ROLE_TYPE_BY_BASIS: dict[str, RoleType] = {
    "route:programme": RoleType.PROGRAMME,
    "route:states-fresh-grad": RoleType.GRADUATE_ROLE,
    "route:entry-level-title": RoleType.GRADUATE_ROLE,
    "route:zero-years": RoleType.ENTRY_LEVEL,
    "route:experience-level": RoleType.ENTRY_LEVEL,
}

# The three a subscriber can choose between. Internships are excluded from the default view
# and are not offered as a subscription option.
SELECTABLE_ROLE_TYPES: tuple[RoleType, ...] = (
    RoleType.PROGRAMME,
    RoleType.GRADUATE_ROLE,
    RoleType.ENTRY_LEVEL,
)


def role_type_for(grad_basis: str, *, is_grad: bool, is_internship: bool) -> RoleType:
    if not is_grad:
        return RoleType.NOT_GRADUATE
    if is_internship:
        return RoleType.INTERNSHIP
    return ROLE_TYPE_BY_BASIS.get(grad_basis, RoleType.GRADUATE_ROLE)


class PostedDateBasis(StrEnum):
    """Where ``posted_date`` came from. Surfaced in the UI next to the date.

    ``PUBLISHED`` is the platform's own publish timestamp and is the only one that means
    what a reader assumes it means. ``UPDATED`` is a modification time standing in for a
    publish time — some platforms expose nothing better. ``OBSERVED`` means we are reporting
    the first day *we* saw it, which for a firm added to the registry last week says more
    about us than about the posting.
    """

    PUBLISHED = "published"
    UPDATED = "updated"
    OBSERVED = "observed"


_ABSOLUTE_URL = re.compile(r"^https?://", re.IGNORECASE)
# Anything that is not alphanumeric collapses to a hyphen, so a requisition id containing a
# slash or a space cannot produce two different keys for one posting on two different days.
_KEY_UNSAFE = re.compile(r"[^a-z0-9]+")


def _key_part(value: str) -> str:
    return _KEY_UNSAFE.sub("-", value.strip().lower()).strip("-")


def make_job_key(platform: str, firm_id: str, external_id: str) -> str:
    """Build the stable cross-snapshot identity for a posting.

    Raises:
        ValueError: if any component is blank after normalisation. A key like ``gh:janestreet:``
            would collide across every unidentified posting on that board and quietly merge
            them into one row.
    """
    parts = [_key_part(platform), _key_part(firm_id), _key_part(external_id)]
    if not all(parts):
        raise ValueError(
            f"job_key needs three non-empty parts; got platform={platform!r} "
            f"firm_id={firm_id!r} external_id={external_id!r}"
        )
    return ":".join(parts)


class SourcePosting(BaseModel):
    """One posting as a source client returns it, before any classification.

    Validated at the ingest boundary. A platform that changes its payload shape must fail
    loudly here rather than write a table full of nulls that looks like a quiet hiring
    freeze.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    firm_id: str
    source_platform: Platform
    external_id: str
    title: str
    apply_url: str
    location_raw: str = ""
    description_text: str = ""
    posted_date: date | None = None
    posted_date_basis: PostedDateBasis | None = None
    department: str = ""
    employment_type: str = ""
    # Platform-specific fields worth keeping but not worth a column: MCF's
    # minimumYearsExperience and positionLevels, Greenhouse's education, Workday's facets.
    # The classifier reads these by name; nothing else may depend on them.
    extra: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("title", "firm_id", "external_id")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("apply_url")
    @classmethod
    def _absolute_url(cls, value: str) -> str:
        cleaned = value.strip()
        if not _ABSOLUTE_URL.match(cleaned):
            raise ValueError(f"apply_url must be an absolute http(s) URL; got {cleaned!r}")
        return cleaned

    @property
    def job_key(self) -> str:
        return make_job_key(self.source_platform, self.firm_id, self.external_id)


# Column order of `data/curated/postings.parquet`. Declared once so the transform, the
# dashboard and the health check cannot drift from each other.
CURATED_COLUMNS: tuple[str, ...] = (
    "job_key",
    "firm_id",
    "firm_name",
    "sector",
    "tier",
    "title",
    "apply_url",
    "source_platform",
    "external_id",
    "location_raw",
    "is_singapore",
    "posted_date",
    "posted_date_basis",
    "first_seen",
    "last_seen",
    "status",
    "job_family",
    "family_group",
    "family_confidence",
    "family_basis",
    "is_grad",
    "grad_confidence",
    "grad_basis",
    "role_type",
    "is_internship",
    "department",
    "employment_type",
    "mcf_job_id",
    "salary_min",
    "salary_max",
    "description_text",
    "snapshot_date",
)
