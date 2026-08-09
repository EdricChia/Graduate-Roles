"""Discovery verification.

Every case here is a board the probe actually found during a live run over the registry.
The wrong ones are the point: a bad token silently attributes another company's jobs to a
firm the user is watching, and unlike a coverage gap, nobody ever notices.
"""

from __future__ import annotations

import pytest

from gradtrack.discover import Hit, candidate_tokens
from gradtrack.firms import Firm, FirmStatus, RobotsStatus
from gradtrack.schema import Platform


def hit(
    firm_id: str,
    firm_name: str,
    platform: Platform,
    token: str,
    jobs: int,
    *,
    board_name: str = "",
    sample_url: str = "",
    sg: int = 0,
) -> Hit:
    return Hit(firm_id, firm_name, platform, token, jobs, sg, board_name, sample_url)


class TestRealMisattributions:
    """Boards that exist, look right, and belong to somebody else."""

    def test_greenhouse_mas_is_midwest_applied_solutions(self) -> None:
        h = hit(
            "mas",
            "Monetary Authority of Singapore",
            Platform.GREENHOUSE,
            "mas",
            4,
            board_name="Midwest Applied Solutions",
            sample_url="https://job-boards.greenhouse.io/mas/jobs/5139946008",
        )
        assert not h.verified

    def test_greenhouse_edb_is_enterprisedb(self) -> None:
        """Nineteen jobs, two in Singapore, and it calls itself "EDB".

        The name check cannot save us here — it agrees. The apply domain is what gives it
        away, and a concrete non-ATS domain that disagrees has to be a hard no.
        """
        h = hit(
            "edb",
            "Singapore Economic Development Board",
            Platform.GREENHOUSE,
            "edb",
            19,
            sg=2,
            board_name="EDB",
            sample_url="https://www.enterprisedb.com/careers/job-openings?gh_jid=7749860003",
        )
        ok, reason = h.verify()
        assert not ok
        assert "too short" in reason or "enterprisedb" in reason

    @pytest.mark.parametrize(
        ("firm_id", "firm_name", "token", "jobs"),
        [
            # Ashby board "applied" — 262 jobs, and not Applied Materials.
            ("appliedmaterials", "Applied Materials", "applied", 262),
            ("arthurdlittle", "Arthur D. Little", "arthur", 1),
            ("jumptrading", "Jump Trading", "jump", 3),
        ],
    )
    def test_a_token_that_is_only_a_prefix_is_refused(
        self, firm_id: str, firm_name: str, token: str, jobs: int
    ) -> None:
        """token_set_ratio scores a subset as a perfect match; these all hit 100 once."""
        assert not hit(firm_id, firm_name, Platform.ASHBY, token, jobs).verified

    @pytest.mark.parametrize(
        ("firm_id", "firm_name", "platform", "token", "jobs"),
        [
            # A sandbox account. Its postings are titled "BO Prim" and "Anirban jobReq 3".
            ("linkedin", "LinkedIn", Platform.LEVER, "linkedin", 23),
            # A German company in Köln and Aachen, not Amber Group.
            ("ambergroup", "Amber Group", Platform.ASHBY, "amber", 22),
        ],
    )
    def test_an_exact_token_with_no_singapore_roles_is_not_corroborated(
        self, firm_id: str, firm_name: str, platform: Platform, token: str, jobs: int
    ) -> None:
        """Both scored a perfect token match and both were the wrong company.

        Brand names are not unique, and on Lever and Ashby there is no company name or
        employer domain to check against.
        """
        h = hit(firm_id, firm_name, platform, token, jobs, sg=0)
        ok, reason = h.verify()
        assert not ok
        assert "Singapore" in reason

    def test_workable_invents_a_board_name_from_the_token(self) -> None:
        """Workable answers 200 for any account and titlecases the token.

        Only the job count separates "Goldman Sachs" the real employer from "goldman-sachs"
        the string Workable just echoed back at us.
        """
        h = hit(
            "goldmansachs",
            "Goldman Sachs",
            Platform.WORKABLE,
            "goldman-sachs",
            0,
            board_name="Goldman Sachs",
        )
        assert not h.verified


class TestGenuineHits:
    """Boards from the same run that are real, and must keep passing."""

    @pytest.mark.parametrize(
        ("firm_id", "firm_name", "board_name", "token", "jobs"),
        [
            ("squarepoint", "Squarepoint Capital", "Squarepoint Capital", "squarepointcapital", 87),
            (
                "towerresearch",
                "Tower Research Capital",
                "Tower Research Capital",
                "towerresearchcapital",
                72,
            ),
            ("point72", "Point72", "Point72 ", "point72", 231),
            ("stripe", "Stripe", "Stripe", "stripe", 550),
            # An abbreviated board name under a longer legal name — the case that stops the
            # subset guard from being a simple exact-match rule.
            ("davinci", "Da Vinci Derivatives", "Da Vinci", "davinciderivatives", 12),
        ],
    )
    def test_board_name_verification(
        self, firm_id: str, firm_name: str, board_name: str, token: str, jobs: int
    ) -> None:
        assert hit(
            firm_id, firm_name, Platform.GREENHOUSE, token, jobs, board_name=board_name
        ).verified

    @pytest.mark.parametrize(
        ("firm_id", "firm_name", "token", "jobs", "sg"),
        [
            ("openai", "OpenAI", "openai", 747, 27),
            ("snowflake", "Snowflake", "snowflake", 395, 8),
            ("palantir", "Palantir", "palantir", 305, 2),
        ],
    )
    def test_exact_token_plus_singapore_roles_verifies(
        self, firm_id: str, firm_name: str, token: str, jobs: int, sg: int
    ) -> None:
        """Lever and Ashby report no company name, so the token plus Singapore presence is
        all the evidence there is — and both halves are required."""
        assert hit(firm_id, firm_name, Platform.ASHBY, token, jobs, sg=sg).verified

    def test_an_employer_domain_that_agrees_verifies(self) -> None:
        h = hit(
            "janestreet",
            "Jane Street",
            Platform.GREENHOUSE,
            "janestreet",
            230,
            sample_url="https://www.janestreet.com/join-jane-street/apply/7642974002",
        )
        assert h.verified


class TestCandidateTokens:
    def test_corporate_noise_is_stripped(self) -> None:
        firm = Firm(
            firm_id="towerresearch",
            firm_name="Tower Research Capital",
            sector="quant",
            tier=1,
            robots_status=RobotsStatus.UNKNOWN,
            status=FirmStatus.TODO,
        )
        tokens = candidate_tokens(firm)
        assert "towerresearch" in tokens
        assert "towerresearchcapital" in tokens

    def test_two_character_tokens_are_never_generated(self) -> None:
        firm = Firm(
            firm_id="ge", firm_name="GE", sector="industrial", tier=2, status=FirmStatus.TODO
        )
        assert all(len(t) >= 3 for t in candidate_tokens(firm))


class TestSingleWordBoardNames:
    """A board named for one common word is not evidence, however well it scores."""

    def test_national_is_not_the_national_environment_agency(self) -> None:
        """greenhouse/national is a public-relations agency in Montreal.

        It scores 100 against "National Environment Agency" because "national" is one of
        that name's tokens, and it clears the five-character guard at eight.
        """
        h = hit(
            "nea",
            "National Environment Agency",
            Platform.GREENHOUSE,
            "national",
            7,
            board_name="NATIONAL",
        )
        ok, reason = h.verify()
        assert not ok

    @pytest.mark.parametrize(
        ("firm_id", "firm_name", "board", "token"),
        [
            ("govtech", "GovTech Singapore", "GovTech ", "govtech"),
            ("thunes", "Thunes", "Thunes", "thunes"),
            ("adyen", "Adyen", "Adyen", "adyen"),
            ("coupang", "Coupang", "Coupang", "coupang"),
            ("teneo", "Teneo", "Teneo ", "teneo"),
            ("davinci", "Da Vinci Derivatives", "Da Vinci", "davinciderivatives"),
        ],
    )
    def test_genuine_one_word_and_multi_word_boards_still_pass(
        self, firm_id: str, firm_name: str, board: str, token: str
    ) -> None:
        assert hit(firm_id, firm_name, Platform.GREENHOUSE, token, 50, board_name=board).verified
