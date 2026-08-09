"""Build the curated tables from the raw snapshot tree.

    uv run python -m gradtrack.transform.build

Reads every platform's partitions, resolves MyCareersFuture employers onto registry firms,
classifies each posting, derives the lifecycle from the full snapshot history, and writes:

* ``data/curated/postings.parquet``            — the table the dashboard and notifier read
* ``data/curated/discovery_candidates.parquet`` — firms posting graduate roles that the
  registry does not cover yet

Nothing here fetches. Rerunning it is free and always reproduces the same output from the
same raw tree, which is the property that makes the raw tree worth keeping append-only.
"""

from __future__ import annotations

import argparse
from datetime import date

import polars as pl

from gradtrack.config import Config, load_config
from gradtrack.firms import Registry, load_registry
from gradtrack.ingest.snapshot import (
    available_snapshots,
    platforms_on_disk,
    read_snapshot_as_postings,
)
from gradtrack.schema import (
    CURATED_COLUMNS,
    FAMILY_GROUPS,
    JobFamily,
    SourcePosting,
    Status,
    role_type_for,
)
from gradtrack.sources.base import contains_singapore
from gradtrack.transform import dedupe
from gradtrack.transform.classify import classify_family, classify_grad
from gradtrack.transform.lifecycle import compute_lifecycle, load_history

POSTINGS_OUT = "postings.parquet"
CANDIDATES_OUT = "discovery_candidates.parquet"

# Declared, not inferred. Polars types a column from the first 100 rows, and `salary_min` is
# null on every ATS row — only MyCareersFuture carries a salary. With 1,400 ATS postings
# ahead of the first MyCareersFuture one, the column was inferred as Null and then the build
# died on "could not append value: 12000.0 of type f64". Inference is also the wrong tool
# here on principle: this table is the project's data contract and its types should be stated
# once, not rediscovered from whatever happens to sort first.
CURATED_SCHEMA: dict[str, pl.DataType] = {
    "job_key": pl.Utf8,
    "firm_id": pl.Utf8,
    "firm_name": pl.Utf8,
    "sector": pl.Utf8,
    "tier": pl.Int64,
    "title": pl.Utf8,
    "apply_url": pl.Utf8,
    "source_platform": pl.Utf8,
    "external_id": pl.Utf8,
    "location_raw": pl.Utf8,
    "is_singapore": pl.Boolean,
    "posted_date": pl.Date,
    "posted_date_basis": pl.Utf8,
    "first_seen": pl.Date,
    "last_seen": pl.Date,
    "status": pl.Utf8,
    "job_family": pl.Utf8,
    "family_group": pl.Utf8,
    "family_confidence": pl.Float64,
    "family_basis": pl.Utf8,
    "is_grad": pl.Boolean,
    "grad_confidence": pl.Float64,
    "grad_basis": pl.Utf8,
    "role_type": pl.Utf8,
    "is_internship": pl.Boolean,
    "department": pl.Utf8,
    "employment_type": pl.Utf8,
    "mcf_job_id": pl.Utf8,
    "salary_min": pl.Float64,
    "salary_max": pl.Float64,
    "description_text": pl.Utf8,
    "snapshot_date": pl.Date,
}


def _as_float(value: object) -> float | None:
    """Salary arrives as int, float, str or None depending on the source."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _split(value: object) -> tuple[str, ...]:
    text = str(value or "")
    return tuple(p for p in text.split("|") if p.strip())


def classify_posting(posting: SourcePosting) -> dict[str, object]:
    """Run both classifiers over one posting, passing through structured signals.

    MyCareersFuture is the only source with structured experience and seniority fields, and
    they arrive in ``extra``. ATS sources pass None, which sends the classifier down the
    prose-only path.
    """
    grad = classify_grad(
        posting.title,
        posting.description_text,
        min_years=_int_or_none(posting.extra.get("min_years")),
        position_levels=_split(posting.extra.get("position_levels")),
        employment_types=_split(posting.extra.get("employment_types"))
        or ((posting.employment_type,) if posting.employment_type else ()),
        # SmartRecruiters calls it experienceLevel, Workable calls it experience. Both were
        # captured into `extra` from the start and neither was being read.
        experience_level=str(
            posting.extra.get("experience_level") or posting.extra.get("experience") or ""
        ),
    )
    family = classify_family(posting.title, posting.description_text, posting.department)
    return {
        "job_family": family.family.value,
        "family_group": family.group,
        "family_confidence": family.confidence,
        "family_basis": family.basis,
        "is_grad": grad.is_grad,
        "grad_confidence": grad.confidence,
        "grad_basis": grad.basis,
        # Derived from the route that admitted it, not recomputed — see schema.RoleType.
        "role_type": role_type_for(
            grad.basis, is_grad=grad.is_grad, is_internship=grad.is_internship
        ).value,
        "is_internship": grad.is_internship,
    }


def load_all_postings(config: Config, snapshot_date: date) -> list[SourcePosting]:
    """Every platform's postings for one date, concatenated."""
    postings: list[SourcePosting] = []
    for platform in platforms_on_disk(config):
        if snapshot_date in available_snapshots(config, platform):
            postings.extend(read_snapshot_as_postings(config, platform, snapshot_date))
    return postings


def latest_snapshot_date(config: Config) -> date | None:
    dates = [d for p in platforms_on_disk(config) for d in available_snapshots(config, p)]
    return max(dates) if dates else None


def build(config: Config, registry: Registry, overrides: dict[str, str]) -> pl.DataFrame:
    """Assemble the curated postings table."""
    latest = latest_snapshot_date(config)
    if latest is None:
        raise RuntimeError("no snapshots on disk; run an ingest job first")

    aliases = dedupe.load_aliases(config.manual_dir / "firm_aliases.csv")
    merged = dedupe.merge_sources(load_all_postings(config, latest), registry, aliases)

    lifecycle = compute_lifecycle(load_history(config))
    firm_index = {firm.firm_id: firm for firm in registry}

    rows: list[dict[str, object]] = []
    for posting in merged.postings:
        classified = classify_posting(posting)
        # A hand-written override always wins over the rule table. That is the documented
        # escape hatch for a title the rules read wrongly, and it must not be re-derived.
        override = overrides.get(posting.job_key)
        if override:
            family = JobFamily(override)
            classified |= {
                "job_family": family.value,
                "family_group": FAMILY_GROUPS[family],
                "family_confidence": 1.0,
                "family_basis": "manual-override",
            }

        life = lifecycle.get(posting.job_key)
        firm = firm_index.get(posting.firm_id)
        rows.append(
            {
                "job_key": posting.job_key,
                "firm_id": posting.firm_id,
                "firm_name": firm.firm_name
                if firm
                else str(posting.extra.get("employer_name") or posting.firm_id),
                "sector": firm.sector if firm else "",
                "tier": firm.tier if firm else 0,
                "title": posting.title,
                "apply_url": posting.apply_url,
                "source_platform": posting.source_platform.value,
                "external_id": posting.external_id,
                "location_raw": posting.location_raw,
                "is_singapore": contains_singapore(posting.location_raw)
                or posting.source_platform.value == "mcf",
                "posted_date": posting.posted_date,
                "posted_date_basis": (
                    posting.posted_date_basis.value if posting.posted_date_basis else None
                ),
                "first_seen": life.first_seen if life else None,
                "last_seen": life.last_seen if life else None,
                "status": life.status.value if life else Status.OPEN.value,
                **classified,
                "department": posting.department,
                "employment_type": posting.employment_type,
                "mcf_job_id": str(posting.extra.get("mcf_job_id") or ""),
                "salary_min": _as_float(posting.extra.get("salary_min")),
                "salary_max": _as_float(posting.extra.get("salary_max")),
                "description_text": posting.description_text[:8000],
                "snapshot_date": latest,
            }
        )

    frame = pl.DataFrame(rows, schema=CURATED_SCHEMA).select(CURATED_COLUMNS)

    config.curated_dir.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(config.curated_dir / POSTINGS_OUT)

    candidates = pl.DataFrame(
        [
            {"employer": name, "postings": count}
            for name, count in dedupe.summarise_candidates(merged.discovery_candidates, top=200)
        ],
        schema={"employer": pl.Utf8, "postings": pl.Int64},
    )
    candidates.write_parquet(config.curated_dir / CANDIDATES_OUT)

    print(f"snapshot {latest}")
    print(
        f"  {len(merged.postings)} postings kept ({merged.merged_count} MCF rows merged into ATS)"
    )
    print(f"  {len(merged.discovery_candidates)} MCF rows from firms not in the registry")
    if frame.height:
        grads = frame.filter(pl.col("is_grad") & ~pl.col("is_internship"))
        print(f"  {grads.height} graduate-level, non-internship postings")
        by_status = grads.group_by("status").len().sort("len", descending=True)
        print(f"  status: {dict(zip(by_status['status'], by_status['len'], strict=True))}")
    return frame


def load_overrides(config: Config) -> dict[str, str]:
    import csv

    path = config.manual_dir / "family_overrides.csv"
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as handle:
        return {
            (row.get("job_key") or "").strip(): (row.get("job_family") or "").strip()
            for row in csv.DictReader(handle)
            if (row.get("job_key") or "").strip() and (row.get("job_family") or "").strip()
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build curated tables from raw snapshots")
    parser.parse_args(argv)
    config = load_config()
    registry = load_registry(config.registry_path)
    build(config, registry, load_overrides(config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
