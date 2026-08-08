"""Workday tenant discovery.

Every case here comes from the live sweep. The verification rule is deliberately stricter
than the evidence sometimes warrants — it rejected Morgan Stanley's real tenant — because the
cost of the two failure modes is not symmetric. A rejected real tenant shows up as a gap and
gets hand-added in a minute; an accepted wrong one silently files another company's postings
under a firm the user is watching.
"""

from __future__ import annotations

import pytest

from gradtrack.discover_workday import WorkdayHit, tenant_candidates
from gradtrack.firms import Firm, FirmStatus


def firm(firm_id: str, name: str) -> Firm:
    return Firm(firm_id=firm_id, firm_name=name, sector="x", tier=1, status=FirmStatus.TODO)


def hit(firm_id: str, name: str, tenant: str, site: str, total: int, sg: int = 0) -> WorkdayHit:
    return WorkdayHit(
        firm_id, name, f"{tenant}.wd3.myworkdayjobs.com", tenant, site, total, sg, False
    )


class TestTenantCandidates:
    def test_the_registry_id_comes_first(self) -> None:
        assert tenant_candidates(firm("dbs", "DBS Bank"))[0] == "dbs"

    def test_corporate_noise_is_stripped(self) -> None:
        assert "micron" in tenant_candidates(firm("micron", "Micron Technology"))

    def test_an_initialism_is_offered(self) -> None:
        """Workday tenants are frequently the internal abbreviation: ms, gs, scb."""
        assert "gs" in tenant_candidates(firm("goldmansachs", "Goldman Sachs"))


class TestVerification:
    def test_a_named_tenant_with_postings_verifies(self) -> None:
        assert hit("dbs", "DBS Bank", "dbs", "DBS_Careers", 266, 18).verified
        assert hit("micron", "Micron Technology", "micron", "External", 878, 862).verified

    def test_an_empty_board_never_verifies(self) -> None:
        """A tenant that exists but serves nothing proves nothing about the site id."""
        assert not hit("shell", "Shell", "shell", "External", 0).verified

    def test_an_initialism_is_refused_for_auto_apply(self) -> None:
        """`ms.wd5/External` really is Morgan Stanley — Investment Banking, Equity Swap Ops.

        It is still refused here, and that is the intended behaviour: two letters cannot
        distinguish Morgan Stanley from anyone else with those initials, and the registry row
        was added by hand after reading the board. Hand-verification is the escape hatch;
        loosening the automated rule is not.
        """
        verdict = hit("morganstanley", "Morgan Stanley", "ms", "External", 34, 13)
        ok, reason = verdict.verify()
        assert not ok
        assert "does not resemble" in reason

    def test_an_unrelated_tenant_is_refused(self) -> None:
        assert not hit("gic", "GIC", "globalinsurance", "External", 500, 20).verified

    @pytest.mark.parametrize(
        ("firm_id", "name", "tenant", "site"),
        [
            # Citi's site id is literally "2". Odd, and confirmed real: it serves Singapore
            # "Salesperson - C12" postings and the apply URL resolves.
            ("citi", "Citi", "citi", "2"),
            # Named "Confidential" but publicly served, with real Mizuho/Greenhill roles.
            ("mizuho", "Mizuho", "mizuho", "Mizuho_Confidential"),
            ("pimco", "PIMCO", "pimco", "pimco-careers"),
        ],
    )
    def test_unusual_site_ids_are_accepted_when_the_board_delivers(
        self, firm_id: str, name: str, tenant: str, site: str
    ) -> None:
        """The site id is opaque and the tenant is what carries the evidence."""
        assert hit(firm_id, name, tenant, site, 291, 20).verified
