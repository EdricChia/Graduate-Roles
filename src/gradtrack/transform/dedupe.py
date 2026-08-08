"""Resolving MyCareersFuture postings onto registry firms, and merging them with ATS rows.

Two jobs, both of which exist because MyCareersFuture is a secondary source.

**Firm gating.** The MyCareersFuture pool is mostly staffing agencies and small businesses.
A 2,979-row sample contained a 56-posting cluster titled exactly "MANAGEMENT ASSOCIATE" from
MORE YOGURT PREMIUM, TOTAL MANPOWER, FOCUS MANPOWER and DAY ONE. Those are correctly
classified as graduate-level roles and are still not something to track. That is not the
classifier's problem to solve — tightening it to exclude them would also exclude real
programmes — it is a question of *whose* jobs we want. So a MyCareersFuture row only enters
the main table if its employer resolves to a firm in the registry. Everything else becomes a
discovery candidate: a firm posting graduate roles in Singapore that we are not yet watching
is a suggestion to add it, which turns the noise into the mechanism that grows coverage.

**Merging.** When the same role appears on both a firm's ATS and MyCareersFuture, the ATS row
wins and keeps its apply link, because firms post to their own board days earlier and the
whole premise of the project is linking to the company's own site. MyCareersFuture
contributes only what the ATS feeds never carry: the salary band and its own job id.

Resolution is deliberately conservative. Attributing another company's postings to a firm the
user is watching is worse than leaving them in the discovery report, because a gap is visible
and a wrong attribution is not.
"""

from __future__ import annotations

import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from rapidfuzz import fuzz

from gradtrack.firms import Registry
from gradtrack.schema import Platform, SourcePosting

# Legal-entity boilerplate. MyCareersFuture reports registered names ("BYTEDANCE PTE. LTD.",
# "ASML SINGAPORE PTE LTD"); the registry holds brands ("ByteDance / TikTok", "ASML").
_ENTITY_NOISE = re.compile(
    r"\b(pte|ltd|limited|llp|llc|inc|plc|private|pvt|co|corp|corporation|company|"
    r"holdings?|group|international|global|asia|apac|pacific|singapore|sg|branch|"
    r"regional|headquarters|hq|s\)|and|the|of)\b",
    re.I,
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Above this on a denoised token-set comparison *and* passing the containment check below.
# 92 rather than 85: at 85, "MICRON" matched "MICRO" competitors in trial runs.
NAME_MATCH_THRESHOLD = 92

# Two postings of the same role can be entered on the ATS and on MyCareersFuture a fortnight
# apart, because board posting follows compliance timing rather than the recruiter.
MERGE_WINDOW = timedelta(days=14)
TITLE_MATCH_THRESHOLD = 88


def denoise(name: str) -> str:
    return _NON_ALNUM.sub(" ", _ENTITY_NOISE.sub(" ", name.lower())).strip()


def compact(name: str) -> str:
    return _NON_ALNUM.sub("", denoise(name))


@dataclass(frozen=True)
class Alias:
    """A hand-recorded mapping from a MyCareersFuture employer to a registry firm.

    UEN is the good one: it is the ACRA registration number, unique and stable, so a UEN
    alias is exact rather than fuzzy. Name aliases exist for employers whose registered name
    shares nothing with the brand.
    """

    kind: str  # "uen" or "name"
    value: str
    firm_id: str


def load_aliases(path: Path | str) -> list[Alias]:
    target = Path(path)
    if not target.exists():
        return []
    with target.open(newline="", encoding="utf-8") as handle:
        return [
            Alias(
                kind=(row.get("alias_kind") or "").strip().lower(),
                value=(row.get("alias_value") or "").strip(),
                firm_id=(row.get("firm_id") or "").strip(),
            )
            for row in csv.DictReader(handle)
            if (row.get("firm_id") or "").strip()
        ]


@dataclass(frozen=True)
class Resolution:
    firm_id: str | None
    basis: str
    score: int = 0


def resolve_firm(posting: SourcePosting, registry: Registry, aliases: list[Alias]) -> Resolution:
    """Map a MyCareersFuture posting's employer onto a registry firm, or decline to.

    Order is by decreasing certainty: an exact UEN, then a hand-written name alias, then an
    exact normalised name, then a high fuzzy score that also passes containment.
    """
    employer = str(posting.extra.get("employer_name") or "")
    uen = str(posting.extra.get("uen") or "").strip()

    for alias in aliases:
        if alias.kind == "uen" and uen and alias.value.upper() == uen.upper():
            return Resolution(alias.firm_id, "alias:uen")
    for alias in aliases:
        if alias.kind == "name" and compact(alias.value) == compact(employer):
            return Resolution(alias.firm_id, "alias:name")

    if not employer.strip():
        return Resolution(None, "no-employer-name")

    employer_compact = compact(employer)
    for firm in registry:
        if employer_compact and employer_compact == compact(firm.firm_name):
            return Resolution(firm.firm_id, "exact-name")

    best: tuple[int, str] = (0, "")
    for firm in registry:
        firm_compact = compact(firm.firm_name)
        if len(firm_compact) < 4:
            # Short brand names generate spurious matches against long legal entity names.
            continue
        score = int(fuzz.token_set_ratio(denoise(firm.firm_name), denoise(employer)))
        if score > best[0]:
            best = (score, firm.firm_id)
        # Containment is the second gate. A high token-set score alone matched unrelated
        # companies that happen to share a common word; requiring the brand to literally
        # appear inside the registered name removes almost all of it.
        if score >= NAME_MATCH_THRESHOLD and firm_compact in employer_compact:
            return Resolution(firm.firm_id, "fuzzy-name", score)

    return Resolution(None, "unmatched", best[0])


@dataclass
class MergeResult:
    postings: list[SourcePosting]
    discovery_candidates: list[SourcePosting]
    merged_count: int
    resolutions: dict[str, Resolution]


def _titles_match(left: str, right: str) -> bool:
    return fuzz.token_set_ratio(left.lower(), right.lower()) >= TITLE_MATCH_THRESHOLD


def _within_window(left: date | None, right: date | None) -> bool:
    # An unknown date must not block a merge; the title and firm already agree.
    if left is None or right is None:
        return True
    return abs(left - right) <= MERGE_WINDOW


def merge_sources(
    postings: list[SourcePosting], registry: Registry, aliases: list[Alias]
) -> MergeResult:
    """Split ATS and MyCareersFuture rows, resolve firms, and fold duplicates together.

    Returns the postings that belong in the curated table, plus the MyCareersFuture rows
    whose employer is not in the registry, which are reported as discovery candidates rather
    than silently dropped.
    """
    ats_rows = [p for p in postings if p.source_platform is not Platform.MCF]
    mcf_rows = [p for p in postings if p.source_platform is Platform.MCF]

    by_firm: dict[str, list[SourcePosting]] = defaultdict(list)
    for row in ats_rows:
        by_firm[row.firm_id].append(row)

    kept: list[SourcePosting] = list(ats_rows)
    candidates: list[SourcePosting] = []
    resolutions: dict[str, Resolution] = {}
    merged = 0
    enrichment: dict[str, SourcePosting] = {}

    for row in mcf_rows:
        resolution = resolve_firm(row, registry, aliases)
        resolutions[row.job_key] = resolution
        if resolution.firm_id is None:
            candidates.append(row)
            continue

        twin = next(
            (
                other
                for other in by_firm.get(resolution.firm_id, [])
                if _titles_match(other.title, row.title)
                and _within_window(other.posted_date, row.posted_date)
            ),
            None,
        )
        if twin is not None:
            enrichment[twin.job_key] = row
            merged += 1
            continue

        # Tracked firm, no ATS twin. Either we have not wired that firm's ATS yet, or the
        # role is genuinely MyCareersFuture-only. Either way it belongs in the table, under
        # the resolved firm rather than the slugged legal entity.
        kept.append(row.model_copy(update={"firm_id": resolution.firm_id}))

    if enrichment:
        kept = [_enrich(row, enrichment.get(row.job_key)) for row in kept]

    return MergeResult(kept, candidates, merged, resolutions)


def _enrich(row: SourcePosting, mcf: SourcePosting | None) -> SourcePosting:
    """Fold a matched MyCareersFuture row's unique fields into the canonical ATS row.

    Only salary and the MyCareersFuture id. Nothing here may touch ``apply_url``: the ATS
    link is the company's own and is the reason this project exists.
    """
    if mcf is None:
        return row
    return row.model_copy(
        update={
            "extra": {
                **row.extra,
                "mcf_job_id": mcf.extra.get("mcf_job_post_id", ""),
                "mcf_url": mcf.apply_url,
                "salary_min": mcf.extra.get("salary_min"),
                "salary_max": mcf.extra.get("salary_max"),
                "salary_type": mcf.extra.get("salary_type", ""),
            }
        }
    )


def summarise_candidates(candidates: list[SourcePosting], top: int = 40) -> list[tuple[str, int]]:
    """Employers posting graduate roles that the registry does not cover, busiest first.

    This is the discovery report. A name appearing here repeatedly is the signal to add a
    registry row.
    """
    counts: dict[str, int] = defaultdict(int)
    for row in candidates:
        name = str(row.extra.get("employer_name") or row.firm_id)
        counts[name] += 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:top]
