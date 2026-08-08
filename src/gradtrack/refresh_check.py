"""Health check. Writes ``reports/health/<date>.md`` and, with ``--check-only``, fails loudly.

    uv run python -m gradtrack.refresh_check
    uv run python -m gradtrack.refresh_check --check-only

The failure this exists to catch is silence. A source that quietly stops returning rows looks
exactly like a quiet week in the job market, and the tracker would keep rendering yesterday's
roles until someone happened to notice. So the check reports, per platform: how many firms
were attempted, how many were readable, how many rows came back, and which firms failed.

Two-step by design, mirroring the finance repo's refresh workflow. The report is written
first and always; only then does ``--check-only`` decide whether to go red. A broken day must
still leave evidence behind.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date

import polars as pl

from gradtrack.config import Config, load_config
from gradtrack.firms import FirmStatus, load_registry
from gradtrack.ingest.snapshot import available_snapshots, platforms_on_disk, read_outcomes

# A platform where every firm failed is broken. A platform where some firms failed is
# ordinary: careers sites go down, and the lifecycle guard already prevents that from
# corrupting the data.
BROKEN_THRESHOLD = 1.0


@dataclass(frozen=True)
class PlatformHealth:
    platform: str
    snapshot_date: date | None
    firms_attempted: int
    firms_ok: int
    rows: int
    failures: list[tuple[str, str]]

    @property
    def failure_rate(self) -> float:
        return 1.0 - (self.firms_ok / self.firms_attempted) if self.firms_attempted else 1.0

    @property
    def broken(self) -> bool:
        if self.snapshot_date is None:
            return True
        return self.failure_rate >= BROKEN_THRESHOLD or self.rows == 0


def platform_health(config: Config, platform: str) -> PlatformHealth:
    dates = available_snapshots(config, platform)
    if not dates:
        return PlatformHealth(platform, None, 0, 0, 0, [])
    latest = dates[-1]
    outcomes = read_outcomes(config, platform, latest)
    if outcomes.is_empty():
        return PlatformHealth(platform, latest, 0, 0, 0, [])
    ok = int(outcomes["ok"].sum())
    rows = int(outcomes["row_count"].sum())
    failures = [
        (row["firm_id"], row["error"] or "unknown error")
        for row in outcomes.iter_rows(named=True)
        if not row["ok"]
    ]
    return PlatformHealth(platform, latest, outcomes.height, ok, rows, failures)


def classifier_health(config: Config) -> tuple[int, int]:
    """Golden-set size and how many rows the current rules get right.

    Reported here as well as in the tests so a weekly run records the number over time —
    a slow drift is invisible in a pass/fail assertion.
    """
    import csv

    from gradtrack.transform.classify import classify_grad

    path = config.manual_dir / "grad_labels.csv"
    if not path.exists():
        return 0, 0
    total = correct = 0
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            total += 1
            verdict = classify_grad(
                row["title"],
                row["desc_excerpt"],
                min_years=int(row["min_years"]) if row["min_years"].strip() else None,
                position_levels=tuple(p for p in row["position_levels"].split("|") if p),
                employment_types=tuple(p for p in row["employment_types"].split("|") if p),
            )
            if verdict.is_grad == (row["is_grad"] == "1"):
                correct += 1
    return total, correct


def build_report(config: Config) -> tuple[str, list[PlatformHealth]]:
    registry = load_registry(config.registry_path)
    healths = [platform_health(config, p) for p in platforms_on_disk(config)]

    lines: list[str] = [f"# Refresh health — {date.today()}", ""]

    lines += [
        "## Sources",
        "",
        "| Platform | Snapshot | Firms | Readable | Rows | Status |",
        "|---|---|---|---|---|---|",
    ]
    for health in healths:
        state = "**BROKEN**" if health.broken else "ok"
        lines.append(
            f"| {health.platform} | {health.snapshot_date or 'never'} | "
            f"{health.firms_attempted} | {health.firms_ok} | {health.rows} | {state} |"
        )

    failures = [(h.platform, firm, err) for h in healths for firm, err in h.failures]
    if failures:
        lines += ["", "## Firms that could not be read", ""]
        lines += [
            "Their postings keep the previous status rather than being marked closed — "
            "see `transform/lifecycle.py`.",
            "",
        ]
        lines += [f"- `{platform}` **{firm}** — {err}" for platform, firm, err in failures[:50]]

    counts = registry.counts_by_status()
    lines += [
        "",
        "## Registry coverage",
        "",
        f"- wired: **{counts.get(FirmStatus.WIRED.value, 0)}**",
        f"- todo (platform not yet identified): {counts.get(FirmStatus.TODO.value, 0)}",
        f"- probed, not used (robots or ToS): {counts.get(FirmStatus.PROBED_NOT_USED.value, 0)}",
        f"- unavailable: {counts.get(FirmStatus.UNAVAILABLE.value, 0)}",
        "",
        f"By platform: {registry.counts_by_platform()}",
    ]

    total, correct = classifier_health(config)
    if total:
        lines += [
            "",
            "## Classifier",
            "",
            f"Graduate accuracy on the golden set: **{correct}/{total}** "
            f"({correct / total:.1%}). Thresholds live in `tests/test_classify.py`.",
        ]

    postings_path = config.curated_dir / "postings.parquet"
    if postings_path.exists():
        frame = pl.read_parquet(postings_path)
        grads = frame.filter(pl.col("is_grad") & ~pl.col("is_internship"))
        by_status = (
            dict(
                zip(
                    *grads.group_by("status").len().sort("len", descending=True),
                    strict=False,
                )
            )
            if grads.height
            else {}
        )
        lines += [
            "",
            "## Curated table",
            "",
            f"- postings: {frame.height}",
            f"- graduate-level, non-internship: {grads.height}",
            f"- by status: {by_status}",
        ]

    candidates_path = config.curated_dir / "discovery_candidates.parquet"
    if candidates_path.exists():
        candidates = pl.read_parquet(candidates_path)
        if candidates.height:
            lines += [
                "",
                "## Discovery candidates",
                "",
                "Employers posting graduate roles in Singapore that the registry does not "
                "cover. Add the recurring ones to `data/firms/registry.csv`.",
                "",
                "| Employer | Postings |",
                "|---|---|",
            ]
            lines += [
                f"| {row['employer']} | {row['postings']} |"
                for row in candidates.head(25).iter_rows(named=True)
            ]

    return "\n".join(lines) + "\n", healths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write the refresh health report")
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="do not write; exit non-zero if any source is broken",
    )
    args = parser.parse_args(argv)

    config = load_config()
    report, healths = build_report(config)

    broken = [h.platform for h in healths if h.broken]

    if args.check_only:
        if broken:
            print(f"BROKEN sources: {', '.join(broken)}")
            return 1
        print(f"all {len(healths)} source(s) healthy")
        return 0

    out = config.raw_dir.parent.parent / "reports" / "health" / f"{date.today()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    print(f"wrote {out}")
    if broken:
        print(f"  broken: {', '.join(broken)} (use --check-only to fail on this)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
