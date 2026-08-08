"""The registry is hand-edited by a human adding firms, so it is validated like input.

These tests exist because the two ways a registry row goes wrong are both silent: a wired
row missing its token fetches nothing and looks like "no graduate openings this week", and a
wired row we are not allowed to fetch is a rule violation nobody notices until the block
arrives.
"""

from __future__ import annotations

import pytest

from gradtrack.config import REPO_ROOT
from gradtrack.firms import Firm, FirmStatus, RobotsStatus, load_registry
from gradtrack.schema import Platform

REGISTRY = REPO_ROOT / "data" / "firms" / "registry.csv"


def _row(**overrides: object) -> dict[str, object]:
    base = {
        "firm_id": "testfirm",
        "firm_name": "Test Firm",
        "sector": "tech",
        "tier": 1,
        "ats_platform": Platform.GREENHOUSE,
        "ats_token": "testfirm",
        "robots_status": RobotsStatus.ALLOWED,
        "status": FirmStatus.WIRED,
    }
    return base | overrides


class TestFirmValidation:
    def test_wired_row_with_everything_is_accepted(self) -> None:
        firm = Firm.model_validate(_row())
        assert firm.is_fetchable

    def test_wired_greenhouse_row_without_a_token_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="needs ats_token"):
            Firm.model_validate(_row(ats_token=""))

    def test_wired_workday_row_needs_host_token_and_site(self) -> None:
        with pytest.raises(ValueError, match="ats_host"):
            Firm.model_validate(_row(ats_platform=Platform.WORKDAY, ats_token="acme"))

    def test_a_disallowed_board_cannot_be_wired(self) -> None:
        with pytest.raises(ValueError, match="robots.txt disallows"):
            Firm.model_validate(_row(robots_status=RobotsStatus.DISALLOWED))

    def test_todo_rows_need_nothing_beyond_identity(self) -> None:
        firm = Firm.model_validate(
            {
                "firm_id": "gic",
                "firm_name": "GIC",
                "sector": "sovereign",
                "tier": 1,
                "ats_platform": None,
                "status": FirmStatus.TODO,
            }
        )
        assert not firm.is_fetchable

    def test_firm_id_must_be_a_slug(self) -> None:
        with pytest.raises(ValueError, match="alphanumeric slug"):
            Firm.model_validate(_row(firm_id="Test Firm!"))


class TestCommittedRegistry:
    """The real file. If these fail, a refresh run would have failed too."""

    def test_it_loads(self) -> None:
        registry = load_registry(REGISTRY)
        assert len(registry) > 100, "the target list should be the full firm universe"

    def test_every_fetchable_firm_resolves_by_id(self) -> None:
        registry = load_registry(REGISTRY)
        for firm in registry.fetchable():
            assert registry.by_id(firm.firm_id) is firm

    def test_the_verified_greenhouse_firms_are_wired(self) -> None:
        registry = load_registry(REGISTRY)
        tokens = {f.firm_id: f.ats_token for f in registry.fetchable(Platform.GREENHOUSE)}
        assert tokens["janestreet"] == "janestreet"
        assert tokens["coinbase"] == "coinbase"

    def test_no_firm_is_wired_without_a_platform(self) -> None:
        registry = load_registry(REGISTRY)
        assert all(f.ats_platform is not None for f in registry.fetchable())
