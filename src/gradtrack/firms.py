"""The firm registry: ``data/firms/registry.csv``, loaded and validated.

This file is the project's core asset. There is one HTTP client per *platform*, never per
firm, so adding a company to the tracker is a CSV row rather than a code change. That is the
only reason "hundreds of firms" is tractable.

Every row also records what we are allowed to do. A firm whose ``robots.txt`` disallows the
board is kept in the registry with ``status = probed_not_used`` rather than deleted, so a
future session does not re-probe it and reach a different answer by accident — the same rule
the finance repo applies to Naver and Yahoo.
"""

from __future__ import annotations

import csv
from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from gradtrack.schema import Platform


class FirmStatus(StrEnum):
    """Whether we actually read this firm, and if not, why not."""

    WIRED = "wired"
    # Feed exists and works, but robots.txt or ToS forbids it. Never fetched.
    PROBED_NOT_USED = "probed_not_used"
    # Probed and there is nothing to read: login wall, no public feed, no SG roles.
    UNAVAILABLE = "unavailable"
    # In the target list, platform not yet identified. Not fetched, but counted as a gap.
    TODO = "todo"


class RobotsStatus(StrEnum):
    ALLOWED = "allowed"
    DISALLOWED = "disallowed"
    UNKNOWN = "unknown"


# What each platform needs before a fetch can even be attempted. Checked at load time so a
# malformed row fails on `uv run pytest`, not at 08:00 in CI halfway through a refresh.
REQUIRED_FIELDS: dict[Platform, tuple[str, ...]] = {
    Platform.GREENHOUSE: ("ats_token",),
    Platform.LEVER: ("ats_token",),
    Platform.ASHBY: ("ats_token",),
    Platform.SMARTRECRUITERS: ("ats_token",),
    Platform.WORKABLE: ("ats_token",),
    # Workday's URL carries the data-centre number (wd1/wd3/wd5...) which is not derivable
    # from the tenant, so the full host is stored rather than reconstructed.
    Platform.WORKDAY: ("ats_host", "ats_token", "board_site"),
    Platform.SUCCESSFACTORS: ("ats_host",),
    Platform.ORACLE_ORC: ("ats_host",),
    Platform.EIGHTFOLD: ("ats_host",),
    Platform.PHENOM: ("ats_host",),
    Platform.BROWSER: ("careers_url",),
    Platform.MCF: (),
}


class Firm(BaseModel):
    """One row of the registry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    firm_id: str
    firm_name: str
    sector: str
    tier: int
    ats_platform: Platform | None = None
    ats_token: str = ""
    ats_host: str = ""
    board_site: str = ""
    careers_url: str = ""
    robots_status: RobotsStatus = RobotsStatus.UNKNOWN
    status: FirmStatus = FirmStatus.TODO
    notes: str = ""

    @field_validator("firm_id")
    @classmethod
    def _slug(cls, value: str) -> str:
        cleaned = value.strip().lower()
        if not cleaned or not cleaned.replace("_", "").replace("-", "").isalnum():
            raise ValueError(f"firm_id must be an alphanumeric slug; got {value!r}")
        return cleaned

    @model_validator(mode="after")
    def _wired_rows_are_complete(self) -> Firm:
        """A wired firm must have everything its platform needs, and be allowed.

        Both halves matter. A wired row missing its token is a silent zero-row fetch that
        looks exactly like "this firm has no graduate openings". A wired row whose robots.txt
        disallows us is a rule violation that nobody would notice until the block arrives.
        """
        if self.status is not FirmStatus.WIRED:
            return self
        if self.ats_platform is None:
            raise ValueError(f"{self.firm_id}: status=wired requires an ats_platform")
        if self.robots_status is RobotsStatus.DISALLOWED:
            raise ValueError(
                f"{self.firm_id}: robots.txt disallows this board, so it cannot be wired. "
                "Set status=probed_not_used and record it in SOURCES.md."
            )
        missing = [
            field
            for field in REQUIRED_FIELDS.get(self.ats_platform, ())
            if not getattr(self, field, "")
        ]
        if missing:
            raise ValueError(
                f"{self.firm_id}: platform {self.ats_platform} needs {', '.join(missing)}"
            )
        return self

    @property
    def is_fetchable(self) -> bool:
        return self.status is FirmStatus.WIRED and self.ats_platform is not None


@dataclass(frozen=True)
class Registry:
    """The loaded registry, plus the lookups every caller ends up wanting."""

    firms: tuple[Firm, ...]

    def __iter__(self):
        return iter(self.firms)

    def __len__(self) -> int:
        return len(self.firms)

    def by_id(self, firm_id: str) -> Firm:
        for firm in self.firms:
            if firm.firm_id == firm_id:
                return firm
        raise KeyError(f"no firm {firm_id!r} in the registry")

    def fetchable(self, platform: Platform | None = None) -> tuple[Firm, ...]:
        """Firms we are allowed to and able to read, optionally filtered to one platform."""
        return tuple(
            firm
            for firm in self.firms
            if firm.is_fetchable and (platform is None or firm.ats_platform == platform)
        )

    def counts_by_status(self) -> dict[str, int]:
        return dict(Counter(firm.status.value for firm in self.firms))

    def counts_by_platform(self) -> dict[str, int]:
        return dict(
            Counter(firm.ats_platform.value for firm in self.firms if firm.ats_platform is not None)
        )


def load_registry(path: Path | str) -> Registry:
    """Read and validate ``registry.csv``.

    Raises:
        FileNotFoundError: if the registry is missing.
        ValueError: on a duplicate ``firm_id``, or any row that fails :class:`Firm`
            validation. Errors are collected and reported together — fixing a hand-edited
            CSV one exception per run is miserable.
    """
    target = Path(path)
    if not target.exists():
        raise FileNotFoundError(f"{target} not found; the firm registry drives every ingest run")

    firms: list[Firm] = []
    errors: list[str] = []
    with target.open(newline="", encoding="utf-8") as handle:
        for line_no, row in enumerate(csv.DictReader(handle), start=2):
            cleaned = {k: (v or "").strip() for k, v in row.items() if k}
            if not cleaned.get("firm_id"):
                continue  # blank separator line
            if not cleaned.get("ats_platform"):
                cleaned["ats_platform"] = None  # type: ignore[assignment]
            try:
                firms.append(Firm.model_validate(cleaned))
            except Exception as exc:  # noqa: BLE001 - collected and re-raised below
                errors.append(f"  line {line_no} ({cleaned.get('firm_id', '?')}): {exc}")

    duplicates = [fid for fid, n in Counter(f.firm_id for f in firms).items() if n > 1]
    if duplicates:
        errors.append(f"  duplicate firm_id: {', '.join(sorted(duplicates))}")

    if errors:
        raise ValueError(f"{target} has {len(errors)} problem(s):\n" + "\n".join(errors))

    return Registry(firms=tuple(firms))
