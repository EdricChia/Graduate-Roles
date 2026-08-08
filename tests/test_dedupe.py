"""Firm resolution: mapping a MyCareersFuture employer onto a registry firm.

This is the gate that keeps the tracker to firms worth watching, and every case below is a
resolution the live pipeline actually produced. The wrong ones matter more than the right
ones: a misattribution puts another company's postings under a name the user trusts, and it
is invisible in a way a coverage gap is not.
"""

from __future__ import annotations

import pytest

from gradtrack.firms import Firm, FirmStatus, Registry
from gradtrack.schema import Platform, SourcePosting
from gradtrack.transform.dedupe import compact, denoise, resolve_firm


def firm(firm_id: str, name: str) -> Firm:
    return Firm(firm_id=firm_id, firm_name=name, sector="x", tier=1, status=FirmStatus.TODO)


REGISTRY = Registry(
    firms=(
        firm("enterprisesg", "Enterprise Singapore"),
        firm("asml", "ASML"),
        firm("macquarie", "Macquarie"),
        firm("bytedance", "ByteDance / TikTok"),
        firm("manulife", "Manulife"),
        firm("qube", "Qube Research & Technologies"),
        firm("ge", "GE"),
        firm("micron", "Micron Technology"),
    )
)


def mcf_posting(employer: str, uen: str = "") -> SourcePosting:
    return SourcePosting(
        firm_id="x",
        source_platform=Platform.MCF,
        external_id="1",
        title="Analyst",
        apply_url="https://www.mycareersfuture.gov.sg/job/1",
        extra={"employer_name": employer, "uen": uen},
    )


class TestMisattributions:
    """Employers the resolver wrongly claimed for a registry firm in a live run."""

    @pytest.mark.parametrize(
        "employer",
        [
            "HONG FOOD ENTERPRISE PTE. LTD.",
            "EMPOWER ENTERPRISE",
            "SGI ENTERPRISE",
            "ZENVORA  NOVA ENTERPRISE",
            "DIAMOND GLASS ENTERPRISE PTE. LTD.",
            "HEWLETT PACKARD ENTERPRISE SINGAPORE PTE. LTD.",
            "ST ENGINEERING ENTERPRISE DIGITAL PTE. LTD.",
        ],
    )
    def test_a_shared_generic_noun_is_not_a_match(self, employer: str) -> None:
        """All seven of these were resolved to Enterprise Singapore, a statutory board.

        Two causes compounded: stripping "Singapore" from the registry side reduced the brand
        to the bare word "enterprise", and containment then matched it anywhere in the name.
        """
        assert resolve_firm(mcf_posting(employer), REGISTRY, []).firm_id != "enterprisesg"

    def test_a_two_letter_brand_needs_an_explicit_alias(self) -> None:
        """ "GE GLOBAL" resolved to GE on an exact-name match after noise stripping.

        Initials are too weak to carry a match on their own; an alias row is the right way to
        claim one.
        """
        assert resolve_firm(mcf_posting("GE GLOBAL"), REGISTRY, []).firm_id is None


class TestGenuineResolutions:
    @pytest.mark.parametrize(
        ("employer", "expected"),
        [
            ("ASML SINGAPORE PTE LTD", "asml"),
            ("BYTEDANCE PTE. LTD.", "bytedance"),
            ("MACQUARIE CAPITAL SECURITIES SINGAPORE PTE LTD", "macquarie"),
            ("QUBE RESEARCH & TECHNOLOGIES SINGAPORE PTE. LTD.", "qube"),
            ("MICRON TECHNOLOGY OPERATIONS PTE. LTD.", "micron"),
        ],
    )
    def test_a_registered_subsidiary_resolves_to_its_brand(
        self, employer: str, expected: str
    ) -> None:
        assert resolve_firm(mcf_posting(employer), REGISTRY, []).firm_id == expected

    def test_an_unknown_employer_stays_unmatched(self) -> None:
        """Which is what makes it a discovery candidate rather than noise in the table."""
        resolution = resolve_firm(mcf_posting("MORE YOGURT PREMIUM PTE. LTD."), REGISTRY, [])
        assert resolution.firm_id is None
        assert resolution.basis == "unmatched"

    def test_a_uen_alias_wins_over_everything(self) -> None:
        from gradtrack.transform.dedupe import Alias

        aliases = [Alias("uen", "200812345A", "enterprisesg")]
        resolution = resolve_firm(
            mcf_posting("SOME UNRELATED NAME", uen="200812345A"), REGISTRY, aliases
        )
        assert resolution.firm_id == "enterprisesg"
        assert resolution.basis == "alias:uen"


class TestDenoise:
    def test_geography_is_stripped_from_employers_only(self) -> None:
        """ "ASML SINGAPORE PTE LTD" is ASML. "Enterprise Singapore" is not "Enterprise"."""
        assert compact("ASML SINGAPORE PTE LTD", strip_geography=True) == "asml"
        assert compact("Enterprise Singapore") == "enterprisesingapore"

    def test_legal_suffixes_are_stripped_from_both(self) -> None:
        assert compact("ByteDance Pte Ltd") == "bytedance"
        assert denoise("Foo Limited").strip() == "foo"
