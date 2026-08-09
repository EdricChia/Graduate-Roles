"""Phenom People career sites, read through their sitemap and JSON-LD.

`GET {host}/{locale}/sitemap_index.xml` → sub-sitemaps → one page per job, each carrying a
`schema.org/JobPosting` block with title, `datePosted`, location and description.

Phenom exposes no public search API — the probes for one all return the site's 404 page — but
it does publish a sitemap, which robots.txt names explicitly. That is a better source than an
undocumented endpoint anyway: it is the list the operator intends crawlers to read, it is
stable, and the JSON-LD on each page is a published standard rather than something reverse
engineered.

Two properties make this cheap despite being page-by-page. The job URL slug carries the
location — `/global/en/job/58603/Associate-Singapore-2027` — so the Singapore filter runs on
the sitemap before a single job page is fetched; BCG lists 887 jobs and 5 of them are in
Singapore. And the links are on the firm's own domain, which among the platforms here only
Greenhouse and SuccessFactors otherwise manage.

The slug is used only to *narrow*. The title comes from the JSON-LD, because
"Associate-Singapore-2027" is a URL, not a job title — the posting is called
"Associate, Singapore (2027)".
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime

import httpx

from gradtrack.config import Config
from gradtrack.firms import Firm
from gradtrack.schema import Platform, PostedDateBasis, SourcePosting
from gradtrack.sources.base import FetchOutcome, RateLimiter

SITEMAP_INDEX = "https://{host}/{locale}/sitemap_index.xml"
# Bounded so a mis-signposted sitemap cannot turn into an unbounded crawl.
MAX_SUBMAPS = 12
MAX_JOB_PAGES = 120

_LOC = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.S)
_JOB_URL = re.compile(r"/job/(\d+)/", re.I)
_LD_JSON = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
_SINGAPORE = re.compile(r"singapore", re.I)
_TAG = re.compile(r"<[^>]+>")


def job_urls(sitemap_xml: str) -> list[str]:
    return [url for url in _LOC.findall(sitemap_xml) if _JOB_URL.search(url)]


def is_singapore_url(url: str) -> bool:
    """Phenom builds the slug from the posting, so the location is in the URL."""
    return bool(_SINGAPORE.search(url))


def parse_job_posting(html: str) -> dict | None:
    """The schema.org JobPosting block, if the page carries one."""
    for block in _LD_JSON.findall(html):
        try:
            data = json.loads(block)
        except ValueError:
            continue
        candidates = data if isinstance(data, list) else [data]
        for item in candidates:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _location_text(posting: dict) -> str:
    location = posting.get("jobLocation")
    entries = location if isinstance(location, list) else [location]
    parts: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        address = entry.get("address")
        if isinstance(address, dict):
            parts += [
                str(address.get(key) or "")
                for key in ("addressLocality", "addressRegion", "addressCountry")
            ]
    return ", ".join(p for p in dict.fromkeys(parts) if p)


def to_source_posting(posting: dict, url: str, firm: Firm) -> SourcePosting | None:
    title = str(posting.get("title") or "").strip()
    match = _JOB_URL.search(url)
    if not title or not match:
        return None

    posted: date | None = None
    raw_date = str(posting.get("datePosted") or "")
    if raw_date:
        try:
            posted = datetime.fromisoformat(raw_date.replace("Z", "+00:00")).date()
        except ValueError:
            posted = None

    employment = posting.get("employmentType")
    if isinstance(employment, list):
        employment = "; ".join(str(e) for e in employment)

    return SourcePosting(
        firm_id=firm.firm_id,
        source_platform=Platform.PHENOM,
        external_id=match.group(1),
        title=title,
        apply_url=url,
        location_raw=_location_text(posting) or "Singapore",
        description_text=_TAG.sub(" ", str(posting.get("description") or "")),
        posted_date=posted,
        posted_date_basis=PostedDateBasis.PUBLISHED if posted else None,
        employment_type=str(employment or ""),
    )


def fetch_firm(
    client: httpx.Client, config: Config, firm: Firm, *, singapore_only: bool = True
) -> tuple[list[SourcePosting], FetchOutcome]:
    """Read one Phenom career site."""
    limiter = RateLimiter(config.rate_limit_for(Platform.PHENOM.value))
    locale = firm.board_site or "global/en"

    def get(url: str) -> str:
        limiter.wait()
        response = client.get(url)
        response.raise_for_status()
        return response.text

    try:
        index = get(SITEMAP_INDEX.format(host=firm.ats_host, locale=locale))
    except Exception as exc:  # noqa: BLE001
        return [], FetchOutcome(
            firm.firm_id, Platform.PHENOM.value, False, 0, f"{type(exc).__name__}: {exc}"[:200]
        )

    submaps = _LOC.findall(index)[:MAX_SUBMAPS] or [
        SITEMAP_INDEX.format(host=firm.ats_host, locale=locale)
    ]
    urls: list[str] = []
    errors: list[str] = []
    for submap in submaps:
        try:
            urls += job_urls(get(submap))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{submap.rsplit('/', 1)[-1]}: {type(exc).__name__}")

    if singapore_only:
        urls = [u for u in urls if is_singapore_url(u)]
    urls = list(dict.fromkeys(urls))[:MAX_JOB_PAGES]

    postings: list[SourcePosting] = []
    for url in urls:
        try:
            posting = parse_job_posting(get(url))
        except Exception:  # noqa: BLE001
            continue
        if posting is None:
            continue
        mapped = to_source_posting(posting, url, firm)
        if mapped is not None:
            postings.append(mapped)

    return postings, FetchOutcome(
        firm.firm_id, Platform.PHENOM.value, True, len(postings), "; ".join(errors[:3])
    )
