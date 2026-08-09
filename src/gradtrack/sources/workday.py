"""Workday CXS client — the highest-value platform here, and the most fragile.

`POST {host}/wday/cxs/{tenant}/{site}/jobs` with
``{"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}``.

Most banks and large MNCs run on Workday, so this is where the bulk of the target list
lives. There is no documented public API; every Workday career site is a single-page app
talking to this endpoint, and it behaves accordingly. Four things it does that will cost you
a day if you do not know them:

* **`limit` above 20 returns an empty ``jobPostings`` array with no error.** Asking for 100
  looks exactly like a firm with no openings. This is the single easiest way to silently
  lose a bank, and it is why ``PAGE_SIZE`` is a constant with this comment attached.
* **The list view returns relative date strings**, not timestamps — "Posted Today", "Posted
  3 Days Ago", "Posted 30+ Days Ago". The last one is unresolvable, so it becomes a null
  date with an ``observed`` basis rather than a guess.
* **It throttles fast paging**, and Akamai bot management blocks naive single-IP loops.
  Hence the deliberately slow 0.5 req/s default in `config.toml.example`.
* **The data-centre number in the host (wd1/wd3/wd5) is not derivable from the tenant**, so
  the registry stores ``ats_host`` in full rather than reconstructing it.

Two things follow from the scale of these tenants, both measured rather than assumed.

**The list is narrowed with ``searchText``, not by paging the world.** Unfiltered totals are
Micron 2,718, NVIDIA 2,000, Salesforce 1,495, DBS 1,353. Searching "Singapore" cuts those to
878, 93, and 266 respectively. Paging 2,718 postings at 20 a page and 0.5 req/s would take
four minutes for one firm before a single description was read.

**Multi-location postings hide their locations.** NVIDIA's Singapore results come back with
``locationsText`` of "2 Locations", so a naive substring filter on that field drops them even
though the search matched. Those rows are kept and resolved from the detail response instead.

Descriptions live behind a second request per posting, and 878 of them for one firm is not
affordable on a twice-daily schedule. So detail fetching runs on a budget, spent first on
titles that look graduate-shaped. The number skipped is reported in the fetch outcome rather
than swallowed — a cap nobody can see reads as full coverage.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

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
    post_json,
)

LIST_URL = "https://{host}/wday/cxs/{tenant}/{site}/jobs"
DETAIL_URL = "https://{host}/wday/cxs/{tenant}/{site}{path}"
PUBLIC_URL = "https://{host}/en-US/{site}{path}"

# Not a tuning knob. Workday silently returns an empty array above 20.
PAGE_SIZE = 20
# 60 pages x 20 = 1,200 postings, which covers every Singapore-narrowed tenant measured so
# far (the largest, Micron, returns 878).
MAX_PAGES = 60
# Free-text narrowing. Workday has no documented location facet id that is stable across
# tenants, but searchText matches location as well as title.
SEARCH_TEXT = "Singapore"

# Descriptions are one request each. This is what a firm may spend per run.
DETAIL_BUDGET = 80

_RELATIVE = re.compile(r"posted\s+(?:(\d+)\+?\s+days?\s+ago|(today)|(yesterday))", re.I)
# "2 Locations", "6 Locations" — an aggregated row whose real locations are in the detail.
_MULTI_LOCATION = re.compile(r"^\s*\d+\s+locations?\s*$", re.I)

# Used only to decide which postings are worth spending a detail request on. Deliberately a
# local pattern rather than an import from transform.classify: ingest must not classify, and
# this is a fetch-budget heuristic, not a verdict. It is intentionally broad — over-including
# costs a request, under-including costs a role.
_WORTH_DETAIL = re.compile(
    r"\b(graduate|grad|campus|intern|trainee|junior|entry|associate|analyst|"
    r"early career|apprentice|programme|program|20\d\d)\b",
    re.I,
)


def parse_posted_on(text: str, today: date) -> tuple[date | None, PostedDateBasis | None]:
    """Turn "Posted 3 Days Ago" into a date.

    "Posted 30+ Days Ago" is deliberately unresolved: the plus means "at least", and
    reporting it as exactly thirty days would put a fabricated date in a column the
    dashboard sorts by. Null with an observed basis is the honest answer.
    """
    match = _RELATIVE.search(text or "")
    if not match:
        return None, None
    days, is_today, is_yesterday = match.groups()
    if is_today:
        return today, PostedDateBasis.PUBLISHED
    if is_yesterday:
        return today - timedelta(days=1), PostedDateBasis.PUBLISHED
    if days and "+" not in text:
        return today - timedelta(days=int(days)), PostedDateBasis.PUBLISHED
    return None, None


class WorkdayPosting(LenientModel):
    title: str
    externalPath: str
    locationsText: str = ""
    postedOn: str = ""
    # Workday puts the requisition id here, as the only element of a one-item list.
    bulletFields: list[str] = Field(default_factory=list)


def _detail_fields(payload: object) -> tuple[str, str, str]:
    """(description, requisition id, resolved location) from a posting detail response.

    The location matters as much as the description here: it is the only way to tell whether
    an aggregated "2 Locations" row actually includes Singapore.
    """
    if not isinstance(payload, dict):
        return "", "", ""
    info = payload.get("jobPostingInfo") or {}
    if not isinstance(info, dict):
        return "", "", ""
    locations = [str(info.get("location") or "")]
    extra = info.get("additionalLocations")
    if isinstance(extra, list):
        locations += [str(item) for item in extra]
    return (
        str(info.get("jobDescription") or ""),
        str(info.get("jobReqId") or ""),
        "; ".join(loc for loc in locations if loc),
    )


def to_source_posting(
    posting: WorkdayPosting,
    firm: Firm,
    today: date,
    description: str,
    req_id: str,
    *,
    resolved_location: str = "",
    site: str = "",
) -> SourcePosting:
    posted, basis = parse_posted_on(posting.postedOn, today)
    return SourcePosting(
        firm_id=firm.firm_id,
        source_platform=Platform.WORKDAY,
        # The external path is stable per requisition and unique within a tenant; the
        # requisition id is not always present in the list view.
        external_id=req_id or posting.externalPath.strip("/").split("/")[-1],
        title=posting.title,
        apply_url=PUBLIC_URL.format(
            host=firm.ats_host, site=site or firm.board_site, path=posting.externalPath
        ),
        # The resolved location wins where we have it: "Singapore - Central" is usable and
        # "2 Locations" is not.
        location_raw=resolved_location or posting.locationsText,
        description_text=description,
        posted_date=posted,
        posted_date_basis=basis,
        extra={
            "requisition_id": req_id or (posting.bulletFields[0] if posting.bulletFields else ""),
            "posted_on_raw": posting.postedOn,
        },
    )


def fetch_firm(
    client: httpx.Client,
    config: Config,
    firm: Firm,
    *,
    singapore_only: bool = True,
    today: date | None = None,
) -> tuple[list[SourcePosting], FetchOutcome]:
    """Read one firm's Workday tenant, across every site listed in ``board_site``.

    A tenant frequently runs several career sites and a graduate tracker wants more than one
    of them. PwC has `Global_Campus_Careers` beside `Global_Experienced_Careers`; Unilever
    has `Unilever_Early_Careers` and `Unilever_UFLP_ULIP_Fast_Track_Career_Site` beside a
    general board and `TMICC`, which is its ice cream business. Forcing one site per firm
    meant choosing between a firm's graduate scheme and everything else it advertises.

    ``board_site`` may therefore be pipe-separated. Postings are deduplicated on ``job_key``,
    so a role listed on two sites is read once.
    """
    sites = [s.strip() for s in firm.board_site.split("|") if s.strip()]
    if len(sites) > 1:
        collected: dict[str, SourcePosting] = {}
        notes: list[str] = []
        ok = False
        for site in sites:
            single = firm.model_copy(update={"board_site": site})
            postings, outcome = fetch_firm(
                client, config, single, singapore_only=singapore_only, today=today
            )
            ok = ok or outcome.ok
            if outcome.error:
                notes.append(f"{site}: {outcome.error}")
            for posting in postings:
                collected.setdefault(posting.job_key, posting)
        return list(collected.values()), FetchOutcome(
            firm.firm_id, Platform.WORKDAY.value, ok, len(collected), "; ".join(notes)[:200]
        )

    site = sites[0] if sites else firm.board_site
    limiter = RateLimiter(config.rate_limit_for(Platform.WORKDAY.value))
    today = today or date.today()
    list_url = LIST_URL.format(host=firm.ats_host, tenant=firm.ats_token, site=site)

    candidates: list[WorkdayPosting] = []
    invalid = 0
    for page in range(MAX_PAGES):
        body = {
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": page * PAGE_SIZE,
            "searchText": SEARCH_TEXT if singapore_only else "",
        }
        try:
            payload = post_json(client, list_url, limiter, json=body)
        except Exception as exc:  # noqa: BLE001
            return [], FetchOutcome(
                firm.firm_id, Platform.WORKDAY.value, False, 0, f"{type(exc).__name__}: {exc}"[:200]
            )
        postings = payload.get("jobPostings", []) if isinstance(payload, dict) else []
        for raw in postings:
            try:
                posting = WorkdayPosting.model_validate(raw)
            except Exception:  # noqa: BLE001
                invalid += 1
                continue
            # An aggregated "2 Locations" row is kept: the search matched Singapore and only
            # the detail response can say which locations those are.
            aggregated = bool(_MULTI_LOCATION.match(posting.locationsText))
            if singapore_only and not aggregated and not contains_singapore(posting.locationsText):
                continue
            candidates.append(posting)
        if len(postings) < PAGE_SIZE:
            break

    # Spend the detail budget on graduate-shaped titles first. An aggregated row always gets
    # a request regardless, because without one we cannot tell whether it is in Singapore at
    # all and would have to guess.
    def priority(posting: WorkdayPosting) -> tuple[int, int]:
        return (
            0 if _MULTI_LOCATION.match(posting.locationsText) else 1,
            0 if _WORTH_DETAIL.search(posting.title) else 1,
        )

    ordered = sorted(candidates, key=priority)
    detailed = ordered[:DETAIL_BUDGET]
    skipped = ordered[DETAIL_BUDGET:]

    results: list[SourcePosting] = []
    for posting in detailed:
        description, req_id, resolved_location = "", "", ""
        try:
            detail = get_json(
                client,
                DETAIL_URL.format(
                    host=firm.ats_host,
                    tenant=firm.ats_token,
                    site=site,
                    path=posting.externalPath,
                ),
                limiter,
            )
            description, req_id, resolved_location = _detail_fields(detail)
        except Exception:  # noqa: BLE001
            # A missing description costs classification accuracy on this row. It is not a
            # reason to drop the row or to fail the firm.
            pass

        # Resolved at last. If the detail says nothing about Singapore, the search hit was on
        # some other field and the row does not belong here.
        aggregated_elsewhere = (
            singapore_only
            and bool(_MULTI_LOCATION.match(posting.locationsText))
            and bool(resolved_location)
            and not contains_singapore(resolved_location)
        )
        if aggregated_elsewhere:
            continue
        results.append(
            to_source_posting(
                posting,
                firm,
                today,
                description,
                req_id,
                resolved_location=resolved_location,
                site=site,
            )
        )

    # Over budget: keep the postings, without descriptions. Losing the row entirely would be
    # worse than classifying it on its title.
    for posting in skipped:
        if singapore_only and _MULTI_LOCATION.match(posting.locationsText):
            continue
        results.append(to_source_posting(posting, firm, today, "", "", site=site))

    notes = []
    if invalid:
        notes.append(f"{invalid} rows failed validation")
    if skipped:
        notes.append(f"{len(skipped)} postings over the detail budget, title-only")
    return results, FetchOutcome(
        firm.firm_id, Platform.WORKDAY.value, True, len(results), "; ".join(notes)
    )
