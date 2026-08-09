"""SAP SuccessFactors (Recruiting Marketing) career-site client.

`GET {host}/tile-search-results/?q=&startrow={n}` — 25 tiles a page, server-rendered HTML.

This one was expected to need a headless browser and does not, which is worth recording
because it removes a 400MB dependency from the critical path. Temasek's site
(`jobs.temasek.com.sg`) renders its job list server-side, and its `robots.txt` disallows only
`/applybutton/`, `/talentcommunity/`, `/services/`, `/preapply/` and similar — the search
paths are explicitly permitted.

It also produces links on the **firm's own domain**, which among the ATS platforms only
Greenhouse otherwise does.

Parsed with regular expressions rather than an HTML parser. That is a deliberate trade: the
Recruiting Marketing template is a stable, machine-generated structure, the fields we need
are individually tagged (`job-id-{id}`, `jobTitle-link`, `section-field location`), and the
alternative is adding a parser dependency for one source. If a second HTML source appears,
revisit it.

Two traps.

**Location is a country code, not a country.** Temasek returns "SG, 238891" — the ISO code
and a postal code. The shared `contains_singapore` helper matches the word "Singapore" and
would drop every row here, so this module does its own check.

**Paging the whole board misses things, so the search is filtered server-side.** RWE's board
was read unfiltered, returned 204 postings and reported zero in Singapore — while RWE was in
fact advertising a "Business Transformation & Strategy Graduate Programme" there. Paging
stopped long before reaching it. `?locationsearch=Singapore` returns those three roles
directly, and is the same lesson as Workday's `searchText`: narrow at the source rather than
walk a global board.

The parameter matters. `?location=Singapore` also works on RWE and returns **zero** on
Temasek, so choosing it would have silently emptied a firm that was working. `locationsearch`
returns the right answer on both. Where a tenant does not support it at all, the first page
comes back empty and the client falls back to the unfiltered sweep rather than reporting a
board with no Singapore roles.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urljoin

import httpx

from gradtrack.config import Config
from gradtrack.firms import Firm
from gradtrack.schema import Platform, PostedDateBasis, SourcePosting
from gradtrack.sources.base import FetchOutcome, RateLimiter

SEARCH_PATH = "/tile-search-results/?q={query}&startrow={start}"
LOCATION_SEARCH_PATH = "/tile-search-results/?q=&locationsearch={location}&startrow={start}"
PAGE_SIZE = 25
MAX_PAGES = 40
LOCATION_FILTER = "Singapore"

_TILE_SPLIT = re.compile(r'<li class="job-tile job-id-')
_TILE_ID = re.compile(r"^(\d+)")
_DATA_URL = re.compile(r'data-url="([^"]+)"')
_TITLE = re.compile(r'class="jobTitle-link[^"]*"[^>]*>\s*(.*?)\s*</a>', re.S)
_SECTION = re.compile(
    r'class="section-field (\w+)[^"]*"[^>]*>(.*?)(?=<div class="section-field|</div>\s*</div>)',
    re.S,
)
_TAGS = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")

# "SG, 238891" is what Temasek returns. Bare "SG" as a whole word, or the country name.
_SINGAPORE = re.compile(r"\bsingapore\b|\bsg\b|\bsgp\b", re.I)
# Recruiting Marketing labels each value with a screen-reader span carrying the field name,
# which then appears in the stripped text. "Location Location SG, 238891" is not a location.
_LABEL_PREFIX = re.compile(
    r"^(location|department|posting date|date|shift ?type|job ?type|category|"
    r"requisition ?id|country|city)\b[:\s]*",
    re.I,
)


def _text(fragment: str) -> str:
    return _WS.sub(" ", html.unescape(_TAGS.sub(" ", fragment))).strip()


def _sections(chunk: str) -> dict[str, str]:
    """Field name to value for one tile, with the duplicated label stripped."""
    out: dict[str, str] = {}
    for name, body in _SECTION.findall(chunk):
        value = _text(body)
        # The label appears twice: once as a screen-reader span, once as the visible label.
        for _ in range(2):
            value = _LABEL_PREFIX.sub("", value).strip()
        if value:
            out[name.lower()] = value
    return out


def is_singapore(location: str) -> bool:
    return bool(_SINGAPORE.search(location or ""))


def _location_from_url(apply_url: str) -> str:
    """Best-effort location out of a Recruiting Marketing job slug.

    `/RWE/job/Singapore-Business-Transformation-.../` puts the location first in the slug.
    Only the leading segment is taken, and only when it names Singapore — guessing a general
    location out of a URL is not reliable, but confirming this one is.
    """
    match = re.search(r"/job/([^/]+)", apply_url or "")
    if not match:
        return ""
    head = match.group(1).split("-")[0]
    return "Singapore" if _SINGAPORE.fullmatch(head) else ""


def parse_tiles(page_html: str, base_url: str, firm: Firm) -> list[SourcePosting]:
    """Extract every job tile from one search-results page."""
    postings: list[SourcePosting] = []
    for chunk in _TILE_SPLIT.split(page_html)[1:]:
        id_match = _TILE_ID.match(chunk)
        url_match = _DATA_URL.search(chunk)
        title_match = _TITLE.search(chunk)
        if not (id_match and url_match and title_match):
            continue
        title = _text(title_match.group(1))
        if not title:
            continue
        fields = _sections(chunk)
        apply_url = urljoin(base_url, html.unescape(url_match.group(1)))
        postings.append(
            SourcePosting(
                firm_id=firm.firm_id,
                source_platform=Platform.SUCCESSFACTORS,
                external_id=id_match.group(1),
                title=title,
                apply_url=apply_url,
                # Not every tenant publishes a location field — RWE's tiles carry no
                # `section-field` blocks at all. The job URL still encodes it, because
                # Recruiting Marketing builds the slug from the posting:
                # /RWE/job/Singapore-Business-Transformation-...
                location_raw=fields.get("location", "") or _location_from_url(apply_url),
                # The tile carries no description; the classifier works from the title and
                # the department. Fetching each detail page would double the request count
                # for a source that is already a secondary priority.
                description_text="",
                posted_date=None,
                posted_date_basis=PostedDateBasis.OBSERVED,
                department=fields.get("department", ""),
                employment_type=fields.get("shifttype", "") or fields.get("jobtype", ""),
                extra={k: v for k, v in fields.items() if k not in {"location", "department"}},
            )
        )
    return postings


def fetch_firm(
    client: httpx.Client, config: Config, firm: Firm, *, singapore_only: bool = True
) -> tuple[list[SourcePosting], FetchOutcome]:
    """Page through one firm's SuccessFactors career site."""
    limiter = RateLimiter(config.rate_limit_for(Platform.SUCCESSFACTORS.value))
    base_url = f"https://{firm.ats_host}"

    def sweep(location: str) -> tuple[list[SourcePosting], str] | None:
        """Page one search. Returns (postings, note), or None if the fetch failed."""
        found: list[SourcePosting] = []
        seen: set[str] = set()
        for page in range(MAX_PAGES):
            start = page * PAGE_SIZE
            path = (
                LOCATION_SEARCH_PATH.format(location=location, start=start)
                if location
                else SEARCH_PATH.format(query="", start=start)
            )
            limiter.wait()
            try:
                response = client.get(base_url + path)
                response.raise_for_status()
            except Exception as exc:  # noqa: BLE001
                return (
                    None if page == 0 else (found, f"stopped at page {page}: {type(exc).__name__}")
                )

            page_postings = parse_tiles(response.text, base_url, firm)
            if not page_postings:
                break
            fresh = 0
            for posting in page_postings:
                if posting.job_key in seen:
                    continue
                seen.add(posting.job_key)
                fresh += 1
                # When the search was filtered server-side, the server already answered the
                # location question. Re-checking a field the tenant may not publish is how
                # all three of RWE's Singapore graduate programmes were discarded after
                # being fetched correctly.
                if singapore_only and not location and not is_singapore(posting.location_raw):
                    continue
                found.append(posting)
            # A site that ignores startrow would serve page one forever. Stopping when a page
            # adds nothing new guards against that becoming forty identical requests.
            if fresh == 0 or len(page_postings) < PAGE_SIZE:
                break
        return found, ""

    note = ""
    if singapore_only:
        result = sweep(LOCATION_FILTER)
        if result is None:
            return [], FetchOutcome(
                firm.firm_id, Platform.SUCCESSFACTORS.value, False, 0, "location search failed"
            )
        postings, note = result
        if postings:
            return postings, FetchOutcome(
                firm.firm_id, Platform.SUCCESSFACTORS.value, True, len(postings), note
            )
        # Zero from a filtered search is ambiguous: the tenant may not support the parameter.
        # Falling back is what stops an unsupported filter from looking like an empty board.
        note = "locationsearch returned nothing; fell back to the unfiltered sweep"

    result = sweep("")
    if result is None:
        return [], FetchOutcome(
            firm.firm_id, Platform.SUCCESSFACTORS.value, False, 0, "search failed"
        )
    postings, sweep_note = result
    return postings, FetchOutcome(
        firm.firm_id,
        Platform.SUCCESSFACTORS.value,
        True,
        len(postings),
        "; ".join(x for x in (note, sweep_note) if x),
    )
