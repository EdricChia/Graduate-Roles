"""The classifier's regression suite. This is the file that must always pass.

Two halves. The first is a set of named traps — each one a posting that appeared in the
2,979-row MyCareersFuture sample and broke an earlier version of the rules. The second
measures precision and recall against the hand-labelled golden set and fails CI if either
slips.

Precision and recall are printed on every run, passing or failing, because a rule change
that holds the thresholds while moving the numbers is still worth seeing in the log.

**What the golden numbers do and do not prove.** Most of the 324 rows were seeded from the
classifier's own output and then reviewed stratum by stratum, with every disagreement fixed
in the rules rather than in the labels. That makes the headline precision and recall a
*regression lock*, not an unbiased accuracy estimate — they are high partly by construction.
The independent evidence is narrower and lives in two places: the twenty hand-written
HARD_CASES, which were written from the postings rather than from predictions, and the named
traps above. Treat a drop in the golden numbers as a real signal and a high absolute value as
weak evidence. An honest accuracy estimate needs a fresh sample labelled blind, which is
worth doing once the ATS legs are wired and the pool is no longer MyCareersFuture-only.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass

import pytest

from gradtrack.config import REPO_ROOT
from gradtrack.schema import JobFamily
from gradtrack.transform.classify import classify_family, classify_grad

GOLDEN = REPO_ROOT / "data" / "manual" / "grad_labels.csv"

# Achieved at the time of writing: precision 1.000, recall 0.994, family accuracy 0.997.
# Set just below, so ordinary rule tuning does not trip them but a genuine regression does.
# Raise them when the classifier genuinely improves — never lower them to make a red build
# green. A rule that cannot hold these numbers is a rule that broke something.
MIN_PRECISION = 0.98
MIN_RECALL = 0.97
MIN_FAMILY_ACCURACY = 0.97


# ---------------------------------------------------------------------------
# Named traps
# ---------------------------------------------------------------------------


class TestGraduateTraps:
    def test_graduate_as_subject_matter_is_rejected(self) -> None:
        """NUS's postgraduate-programme administrator matches every naive keyword filter."""
        v = classify_grad("Executive, Graduate Studies' Office, School of Computing", min_years=1)
        assert not v.is_grad
        assert v.basis == "veto:graduate-as-subject-matter"

    def test_a_postdoc_is_not_a_graduate_hire(self) -> None:
        v = classify_grad("Research Fellow (Power Grids and Markets)", min_years=0)
        assert not v.is_grad
        assert v.basis == "veto:academic-track"

    def test_three_years_beats_a_graduate_title(self) -> None:
        """Asahi's "Go Graduate Finance" demands three years."""
        v = classify_grad("Go Graduate Finance", min_years=3)
        assert not v.is_grad
        assert v.basis == "veto:3-years-required"

    def test_professional_level_does_not_disqualify(self) -> None:
        """ByteDance tags all ~25 of its Singapore graduate engineering roles Professional.

        Filtering on "Fresh/entry level" would drop the largest graduate employer in the
        sample, which is why position level is a weight and never a gate.
        """
        v = classify_grad(
            "Backend Engineer Graduate (ShortText Recommendation) - 2026 Start",
            min_years=0,
            position_levels=("Professional",),
        )
        assert v.is_grad

    def test_one_year_still_counts_as_graduate(self) -> None:
        """Leonteq's "Graduate, Trading" and Merck's GOglobal are both tagged 1."""
        assert classify_grad("Graduate, Trading", min_years=1).is_grad
        assert classify_grad("Associate, GOglobal Graduate Program", min_years=1).is_grad

    def test_management_associate_is_a_programme_only_when_it_is_the_whole_role(self) -> None:
        """Shinhan's "Risk Management Associate" merely contains the substring."""
        assert classify_grad(
            "MANAGEMENT ASSOCIATE (TRAINEE MANAGER)",
            min_years=0,
            position_levels=("Fresh/entry level",),
        ).is_grad
        assert not classify_grad(
            "Risk Management Associate/Assistant Manager",
            min_years=1,
            position_levels=("Executive",),
        ).is_grad

    def test_two_structured_signals_override_a_programme_title(self) -> None:
        """The staffing-agency "MANAGEMENT ASSOCIATE" cluster: 2 years + Senior Management."""
        v = classify_grad(
            "MANAGEMENT ASSOCIATE", min_years=2, position_levels=("Senior Management",)
        )
        assert not v.is_grad

    def test_an_explicit_fresh_grad_claim_survives_a_seniority_word(self) -> None:
        v = classify_grad(
            "#2 Ola Trainee - AI Product Manager (Fresh Grads only)",
            min_years=0,
            position_levels=("Fresh/entry level",),
        )
        assert v.is_grad
        assert v.basis == "route:states-fresh-grad"

    def test_a_dual_rung_posting_keeps_its_junior_half(self) -> None:
        assert classify_grad(
            "Senior Engineer/ Engineer, RD Packaging & Module Engineering", min_years=0
        ).is_grad
        assert not classify_grad("Senior Data Analyst #ESY", min_years=2).is_grad

    def test_zero_years_alone_qualifies(self) -> None:
        """The third leg of scope: entry-level roles with zero years in the required range."""
        v = classify_grad("Data Analyst", min_years=0, position_levels=("Junior Executive",))
        assert v.is_grad
        assert v.basis == "route:zero-years"

    def test_structured_signals_alone_never_qualify(self) -> None:
        """An earlier scoring version accepted these on years+level with no evidence."""
        for title in ("Retail Associate", "Front Office Associate (Hotel)"):
            v = classify_grad(title, min_years=1, position_levels=("Fresh/entry level",))
            assert not v.is_grad, f"{title} should not qualify on structured signals alone"

    def test_company_history_is_not_an_experience_requirement(self) -> None:
        """Point72's graduate programme was vetoed at thirty years.

        Its boilerplate reads "building on more than 30 years of investing experience". The
        veto has to read requirements, not the firm's own age.
        """
        verdict = classify_grad(
            "Point72 Academy Investment Analyst Program for Upcoming Graduates (2027 - SG)",
            "building on more than 30 years of investing experience, point72 seeks to deliver "
            "superior returns",
        )
        assert verdict.is_grad
        assert verdict.basis == "route:programme"

    def test_a_real_experience_requirement_still_vetoes(self) -> None:
        """The history exemption must not disarm the veto generally."""
        assert not classify_grad(
            "Software Engineer", "minimum of 3 years of experience in backend systems"
        ).is_grad
        assert not classify_grad("Analyst", "we require at least 5 years of experience").is_grad

    def test_a_fresh_grad_phrase_that_points_elsewhere_does_not_count(self) -> None:
        """Jump Trading's "Quantitative Researcher | Trading Team" is an experienced hire.

        Its body says "if you are currently a student or recent graduate, please see our
        campus postings which offer both intern and full-time opportunities" — the phrase is
        there to send graduates away, and reading it as an invitation inverts the sentence.
        """
        verdict = classify_grad(
            "Quantitative Researcher | Trading Team",
            "reliable and predictable availability. if you are currently a student or recent "
            "graduate, please see our campus postings which offer both intern and full-time "
            "opportunities.",
        )
        assert not verdict.is_grad

    @pytest.mark.parametrize(
        "description",
        [
            "this role is not intended for recent graduates",
            "for graduate opportunities please visit our university programmes page",
        ],
    )
    def test_other_redirect_and_exclusion_phrasings(self, description: str) -> None:
        assert not classify_grad("Trader", description).is_grad

    @pytest.mark.parametrize(
        "description",
        [
            "we welcome applications from fresh graduates and final year students",
            "no prior experience required; recent graduates encouraged to apply",
        ],
    )
    def test_a_genuine_invitation_still_counts(self, description: str) -> None:
        """The redirect guard must not disarm the route it protects."""
        verdict = classify_grad("Analyst", description)
        assert verdict.is_grad
        assert verdict.basis == "route:states-fresh-grad"

    def test_bare_campus_counts_as_a_programme(self) -> None:
        """Jump Trading titles its graduate roles "Campus AI/ML Researcher (Fall 2026)".

        A pattern requiring "campus hire" or "campus recruit" missed six of them.
        """
        assert classify_grad("Campus AI/ML Researcher (Fall 2026)").is_grad
        assert classify_grad("Campus Quantitative Trader").is_grad

    def test_a_recruitment_event_is_not_a_role(self) -> None:
        """Temasek lists "Campus Recruitment Event ... Networking Event" among its postings."""
        verdict = classify_grad(
            "Campus Recruitment Event - Temasek Countrywide Networking Event (Singapore)"
        )
        assert not verdict.is_grad
        assert verdict.basis == "veto:not-a-role"

    def test_a_platforms_entry_level_tag_qualifies_on_its_own(self) -> None:
        """SmartRecruiters' experienceLevel and Workable's experience were captured into
        `extra` from the first commit and never read, so a posting the employer had tagged
        "Entry Level" was judged on its prose alone."""
        verdict = classify_grad("Business Analyst", experience_level="Entry Level")
        assert verdict.is_grad
        assert verdict.basis == "route:experience-level"

    @pytest.mark.parametrize("level", ["Mid-Senior Level", "Executive", "Director"])
    def test_a_senior_experience_level_vetoes(self, level: str) -> None:
        assert not classify_grad("Business Analyst", experience_level=level).is_grad

    def test_a_named_programme_outranks_a_careless_level_tag(self) -> None:
        """A firm can tag its graduate scheme wrongly; the programme name is the better
        evidence, so the level veto does not apply to it."""
        assert classify_grad(
            "Graduate Analyst Programme", experience_level="Mid-Senior Level"
        ).is_grad

    def test_an_ambiguous_level_decides_nothing(self) -> None:
        """ "Associate" is a title in banking and a seniority band elsewhere."""
        assert not classify_grad("Business Analyst", experience_level="Associate").is_grad

    def test_a_zero_floor_range_qualifies_regardless_of_its_ceiling(self) -> None:
        """BCG's "Associate, Singapore (2027)" asks for "work experience of 0-3 years".

        That is MBB's undergraduate entry role. The range was capped at 0-2, so a floor of
        zero and a ceiling of three read as no signal at all and the role was dropped. The
        ceiling says how far the firm will stretch for a lateral hire; the floor is the part
        that says a graduate may apply.
        """
        for ceiling in (1, 2, 3, 5):
            verdict = classify_grad(
                "Associate, Singapore (2027)",
                f"what you'll bring: work experience of 0-{ceiling} years in top tier firms",
            )
            assert verdict.is_grad, ceiling
            assert verdict.basis == "route:states-fresh-grad"

    def test_a_stated_experience_floor_above_zero_still_vetoes(self) -> None:
        """Widening the ceiling must not turn "3-5 years" into a graduate role."""
        assert not classify_grad("Associate", "we want 3-5 years of experience").is_grad

    def test_naming_the_school_leaver_route_counts_as_an_invitation(self) -> None:
        """BCG's Consultant postings carry no other graduate signal in 3,900 characters.

        "if you are joining us directly from school or with a few years of experience" is
        the whole of it — one of the two named entry paths is leaving university.
        """
        verdict = classify_grad(
            "Consultant, Singapore",
            "if you are joining us directly from school or with a few years of experience, "
            "expect to spend time working across a wide range of clients.",
        )
        assert verdict.is_grad
        assert verdict.basis == "route:states-fresh-grad"

    def test_an_ats_row_with_no_grad_wording_is_not_a_graduate_role(self) -> None:
        """Jane Street's Singapore postings carry no structured field and claim nothing."""
        assert not classify_grad("Software Engineer").is_grad
        assert not classify_grad("Cybersecurity Engineer").is_grad


class TestInternshipDetection:
    def test_title_internship(self) -> None:
        assert classify_grad("Amplify Program Audit & Assurance Graduate Intern").is_internship

    def test_employment_type_internship(self) -> None:
        """Some postings only disclose it in employmentTypes."""
        v = classify_grad("Research Analyst Programme", employment_types=("Internship/Attachment",))
        assert v.is_internship

    def test_management_trainee_is_not_an_internship(self) -> None:
        """In Singapore "Management Trainee" is a graduate programme, not an internship."""
        v = classify_grad("Management Trainee", min_years=0)
        assert v.is_grad
        assert not v.is_internship


class TestFamily:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Quantitative Trader", JobFamily.QUANT_TRADING),
            ("Junior Quant Researcher", JobFamily.QUANT_RESEARCH),
            ("Quantitative Developer", JobFamily.QUANT_DEV),
            ("Strategy & Operations Associate", JobFamily.STRATEGY_OPERATIONS),
            ("Graduate Strategy and Execution Consultant", JobFamily.STRATEGY_CONSULTING),
            ("Associate Consultant", JobFamily.MANAGEMENT_CONSULTING),
            ("Supply Chain Operations Analyst", JobFamily.SUPPLY_CHAIN),
            ("Machine Learning Engineer Graduate", JobFamily.DATA_SCIENCE),
            ("Big Data Engineer Graduate", JobFamily.SOFTWARE_ENGINEERING),
            ("Data Analyst", JobFamily.DATA_ANALYST),
            ("Business Analyst (Bank)", JobFamily.BUSINESS_ANALYST),
            ("Backend Software Engineer", JobFamily.SOFTWARE_ENGINEERING),
            ("Investment Banking Analyst", JobFamily.INVESTMENT),
            ("Internal Audit Associate", JobFamily.RISK_COMPLIANCE),
            ("HR Graduate Programme", JobFamily.HUMAN_RESOURCES),
            ("Graduate Management Associate", JobFamily.GENERAL_MANAGEMENT),
            ("Process Engineer", JobFamily.ENGINEERING),
        ],
    )
    def test_title_routing(self, title: str, expected: JobFamily) -> None:
        assert classify_family(title).family is expected

    def test_supply_chain_beats_operations(self) -> None:
        """Ordering test. "Operations" would otherwise swallow the compound title."""
        assert classify_family("Supply Chain Operations Executive").family is JobFamily.SUPPLY_CHAIN

    def test_data_science_beats_software_engineering(self) -> None:
        assert classify_family("Machine Learning Engineer").family is JobFamily.DATA_SCIENCE

    def test_software_engineering_beats_bare_engineering(self) -> None:
        assert classify_family("Site Reliability Engineer").family is JobFamily.SOFTWARE_ENGINEERING

    def test_a_title_match_outranks_a_description_match(self) -> None:
        verdict = classify_family(
            "Supply Chain Analyst", description="you will work closely with our data science team"
        )
        assert verdict.family is JobFamily.SUPPLY_CHAIN
        assert verdict.basis == "title"


# ---------------------------------------------------------------------------
# Golden set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Labelled:
    firm: str
    title: str
    min_years: int | None
    position_levels: tuple[str, ...]
    employment_types: tuple[str, ...]
    description: str
    is_grad: bool
    is_internship: bool
    family: str
    note: str


def _split(value: str) -> tuple[str, ...]:
    return tuple(p for p in value.split("|") if p.strip())


def load_golden() -> list[Labelled]:
    with GOLDEN.open(newline="", encoding="utf-8") as handle:
        return [
            Labelled(
                firm=row["firm"],
                title=row["title"],
                min_years=int(row["min_years"]) if row["min_years"].strip() else None,
                position_levels=_split(row["position_levels"]),
                employment_types=_split(row["employment_types"]),
                description=row["desc_excerpt"],
                is_grad=row["is_grad"] == "1",
                is_internship=row["is_internship"] == "1",
                family=row["job_family"],
                note=row["note"],
            )
            for row in csv.DictReader(handle)
        ]


class TestFamilySectorResidual:
    def test_a_consulting_firms_unmatched_title_is_consulting(self) -> None:
        """BCG's core graduate job is titled "Associate, Singapore (2027)".

        Nothing in it says consulting, so it landed in Other — outside the one group most
        worth subscribing to. At a consulting firm the generalist grade is the consulting job.
        """
        verdict = classify_family("Associate, Singapore (2027)", sector="consulting")
        assert verdict.family is JobFamily.MANAGEMENT_CONSULTING
        assert verdict.basis == "sector-residual"

    def test_the_residual_never_overrides_a_rule_that_matched(self) -> None:
        """PwC is a consulting firm whose graduate intake is mostly tax and audit.

        The residual runs only after title, department and description have all failed, so
        those rows keep the family their titles state.
        """
        for title, family in [
            ("Tax - Corporate Tax Associate (July 2027 Intake)", JobFamily.FINANCE_ACCOUNTING),
            ("General Assurance - Accountancy Associate", JobFamily.RISK_COMPLIANCE),
            ("Consulting - SAP BTP Developer Associate", JobFamily.SOFTWARE_ENGINEERING),
        ]:
            verdict = classify_family(title, sector="consulting")
            assert verdict.family is family, title
            assert verdict.basis == "title"

    def test_a_description_match_also_outranks_the_residual(self) -> None:
        """PwC's "Risk Services - Cybersecurity Associate" carries no family word its title
        rules recognise and is classified from its body, which the residual must not pre-empt.
        """
        verdict = classify_family(
            "Risk Services - Cybersecurity Associate",
            "you will join our technology risk and compliance practice",
            sector="consulting",
        )
        assert verdict.family is JobFamily.RISK_COMPLIANCE
        assert verdict.basis == "description"

    def test_other_sectors_keep_falling_through_to_other(self) -> None:
        """Only consulting has an unambiguous residual. A quant firm's unmatched posting is
        as likely to be recruiting as trading."""
        for sector in ("quant", "bank", "bigtech", ""):
            assert classify_family("Associate", sector=sector).family is JobFamily.OTHER

    def test_case_team_is_consulting_vocabulary(self) -> None:
        """Roland Berger's "Case Team Assistant" was unmatched. A case team is a consulting
        firm's unit of work and the phrase means nothing anywhere else."""
        verdict = classify_family("Case Team Assistant")
        assert verdict.family is JobFamily.MANAGEMENT_CONSULTING
        assert verdict.basis == "title"


class TestGoldenSet:
    def test_the_golden_set_is_big_enough_to_mean_something(self) -> None:
        golden = load_golden()
        assert len(golden) >= 300
        assert sum(1 for row in golden if row.is_grad) >= 100
        assert sum(1 for row in golden if not row.is_grad) >= 100

    def test_grad_precision_and_recall(self, capsys: pytest.CaptureFixture[str]) -> None:
        golden = load_golden()
        tp = fp = fn = tn = 0
        misses: list[str] = []
        for row in golden:
            predicted = classify_grad(
                row.title,
                row.description,
                min_years=row.min_years,
                position_levels=row.position_levels,
                employment_types=row.employment_types,
            ).is_grad
            if predicted and row.is_grad:
                tp += 1
            elif predicted and not row.is_grad:
                fp += 1
                misses.append(f"  FP  {row.firm[:24]:<24} {row.title[:56]}")
            elif not predicted and row.is_grad:
                fn += 1
                misses.append(f"  FN  {row.firm[:24]:<24} {row.title[:56]}")
            else:
                tn += 1

        precision = tp / (tp + fp) if tp + fp else 1.0
        recall = tp / (tp + fn) if tp + fn else 1.0
        with capsys.disabled():
            print(
                f"\n  grad: precision={precision:.3f} recall={recall:.3f} "
                f"(tp={tp} fp={fp} fn={fn} tn={tn}, n={len(golden)})"
            )
            for line in misses[:15]:
                print(line)

        assert precision >= MIN_PRECISION, f"precision {precision:.3f} < {MIN_PRECISION}"
        assert recall >= MIN_RECALL, f"recall {recall:.3f} < {MIN_RECALL}"

    def test_internship_detection_on_the_golden_set(self) -> None:
        golden = load_golden()
        wrong = [
            row.title
            for row in golden
            if classify_grad(
                row.title,
                row.description,
                min_years=row.min_years,
                position_levels=row.position_levels,
                employment_types=row.employment_types,
            ).is_internship
            != row.is_internship
        ]
        assert not wrong, f"internship flag wrong on: {wrong[:10]}"

    def test_family_accuracy(self, capsys: pytest.CaptureFixture[str]) -> None:
        golden = load_golden()
        correct = 0
        wrong: list[str] = []
        for row in golden:
            predicted = classify_family(row.title, row.description).family.value
            if predicted == row.family:
                correct += 1
            else:
                wrong.append(f"  {row.title[:50]:<50} got={predicted} want={row.family}")
        accuracy = correct / len(golden)
        with capsys.disabled():
            print(f"  family: accuracy={accuracy:.3f} ({correct}/{len(golden)})")
            for line in wrong[:10]:
                print(line)
        assert accuracy >= MIN_FAMILY_ACCURACY, f"accuracy {accuracy:.3f} < {MIN_FAMILY_ACCURACY}"

    def test_the_hand_written_hard_cases_all_pass(self) -> None:
        """These are the rows that encode a specific bug. None may regress, ever."""
        failures = []
        for row in load_golden():
            if not row.note or row.note.startswith("reviewed:"):
                continue
            verdict = classify_grad(
                row.title,
                row.description,
                min_years=row.min_years,
                position_levels=row.position_levels,
                employment_types=row.employment_types,
            )
            if verdict.is_grad != row.is_grad:
                failures.append(
                    f"{row.title[:52]!r}: got is_grad={verdict.is_grad} "
                    f"({verdict.basis}), want {row.is_grad} — {row.note}"
                )
        assert not failures, "hard cases regressed:\n" + "\n".join(failures)
