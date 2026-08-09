"""Graduate eligibility and job family, as pure functions.

This is the hardest correctness problem in the repo and the one that fails silently in both
directions: a false negative means a role you wanted never reaches your phone, a false
positive means the tracker fills with noise until you stop reading it.

Every rule below was derived from a 2,979-posting sample captured from MyCareersFuture on
2026-08-08, plus the Jane Street and Coinbase Greenhouse boards. The comments name the
postings that justify each rule, because a rule without a counterexample behind it is a
guess and the next person to touch this needs to know which is which.

**Eligibility is decided by named routes, not by a score crossing a line.** The scope has
three distinct legs — graduate programmes, graduate-level openings, and entry-level roles
stating fresh-graduate or zero years — and collapsing them into one number made the
threshold do work it could not explain. A pure score accepted "Retail Associate" and "Front
Office Associate (Hotel)" on nothing but ``years:1 + level:fresh/entry``, which is two weak
structured signals and no evidence at all. Routes make each acceptance state its reason, and
``grad_basis`` carries that reason into the dashboard.

The score is still computed, but only as a confidence for ranking. It never decides.

No I/O. The rule tables are module constants and the golden set lives in the tests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from gradtrack.schema import FAMILY_GROUPS, JobFamily

# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_ENTITIES = {"&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">", "&#39;": "'", "&quot;": '"'}


def clean_text(value: str | None) -> str:
    """Strip HTML to a flat lowercase string suitable for regex matching."""
    if not value:
        return ""
    text = _TAG.sub(" ", value)
    for entity, replacement in _ENTITIES.items():
        text = text.replace(entity, replacement)
    return _WS.sub(" ", text).strip().lower()


# ---------------------------------------------------------------------------
# Vetoes — these fire before any route and nothing overrides them except where
# explicitly noted.
# ---------------------------------------------------------------------------

# "graduate" as a subject matter rather than a seniority. NUS posts "Executive, Graduate
# Studies' Office, School of Computing" — an administrator for a postgraduate programme,
# requiring experience, matching every naive keyword filter.
SUBJECT_MATTER_GRADUATE = re.compile(
    r"\bgraduate (stud(y|ies)|school|admission|programme office|program office|affairs|"
    r"educat|recruit|coordinator)\b|\bpostgraduate (stud|admission|programme)",
    re.I,
)

# Academic career-track roles. A Research Fellow is a postdoc, not a graduate hire, and NTU
# posts several whose descriptions mention graduate study.
# Not a job. Careers sites list these alongside postings and they match graduate patterns
# perfectly — Temasek advertises a "Campus Recruitment Event - Temasek Countrywide Networking
# Event (Singapore)", which is worth knowing about but is not a role to apply for.
#
# It also catches test requisitions left live on a tenant. DBS's Workday board carries
# "[JIM LV TEST – Do NOT Apply] 2027 Management Associate", which is a perfect graduate
# title attached to a posting that explicitly says not to apply.
NOT_A_ROLE = re.compile(
    r"\b(recruitment event|networking event|info(rmation)? session|open house|career fair|"
    r"webinar|meet[- ]?(the|and)[- ]?(team|greet)|talent (community|network|pool)|"
    r"expression of interest|speculative application|do not apply|test (post|req|requisition))\b"
    r"|\[[^\]]*\btest\b[^\]]*\]",
    re.I,
)

ACADEMIC_TRACK = re.compile(
    r"\b(research fellow|post[- ]?doc|postdoctoral|adjunct|professor|lecturer|tutor|"
    r"teaching assistant|dean|provost)\b",
    re.I,
)

# Seniority in the title. Word boundaries matter enormously: `\bmanager\b` must not match
# "Management Associate" or "Management Trainee", which are the standard Singapore names for
# a graduate programme. "Assistant Manager, Academic (Graduate Programmes)" at five years is
# the posting this rule exists to kill.
SENIORITY = re.compile(
    r"\b(senior|snr|lead|principal|staff|head|director|manager|managing|chief|"
    r"vice president|vp|svp|evp|avp|partner|supervisor|superintendent|expert|iii|iv)\b",
    re.I,
)

# Three years is the veto threshold because the captured sample contains no genuine graduate
# posting above two. It is what kills "Go Graduate Finance" (Asahi, 3 years) and
# "CAMPAIGN MANAGEMENT TRAINEE" (3 years), both of which carry strong graduate titles.
YEARS_VETO = 3

# Explicit experience demands in prose, for sources with no structured field.
YEARS_DEMANDED = re.compile(
    r"\b(?:at least |minimum (?:of )?|min\.? )?(\d{1,2})\+?\s*(?:-\s*\d{1,2}\s*)?"
    r"(?:years?|yrs?)\b[^.]{0,40}?\b(?:experience|exp\b)",
    re.I,
)

# Company history, not a requirement — and the difference nearly cost a real graduate
# programme. Point72's "Academy Investment Analyst Program for Upcoming Graduates (2027 –
# SG)" was vetoed at thirty years because its boilerplate reads "building on more than 30
# years of investing experience". Anything introduced by one of these is describing the firm.
#
# The asymmetry here is deliberate. Skipping the veto on a genuinely senior role that says
# "candidates with over 5 years of experience" costs almost nothing, because with no
# graduate signals it still falls through to `no-route`. Applying the veto to a graduate
# programme loses the role outright.
YEARS_AS_HISTORY = re.compile(
    r"\b(?:over|more than|for|nearly|almost|past|last|about|around|"
    r"founded|since|history of|track record of|experience of)\s+$",
    re.I,
)

# ---------------------------------------------------------------------------
# Route signals
# ---------------------------------------------------------------------------

# A named graduate programme, or a title that says "graduate" outright. The "- 2026 Start"
# suffix earns its own alternative: ByteDance uses it on every one of its ~25 Singapore
# graduate engineering postings, all of which are tagged positionLevel "Professional" and
# would be missed by any entry-level filter.
PROGRAMME_TITLE = re.compile(
    # Bare "campus", not "campus hire". Jump Trading titles its graduate roles "Campus AI/ML
    # Researcher (Fall 2026)" and "Campus Quantitative Trader" — six roles that the narrower
    # pattern missed entirely.
    r"\b(graduate|new grad|newgrad|fresh grad(uate)?|campus|"
    r"analyst programme|analyst program|associate programme|associate program|"
    r"rotational programme|rotational program|leadership programme|leadership program|"
    r"development programme|development program|early career(s)?|apprentice(ship)?)\b"
    r"|\b20\d\d\s*(start|intake|cohort)\b|\bclass of 20\d\d\b",
    re.I,
)

# "Management Associate" and "Management Trainee" are the standard Singapore names for a
# graduate programme, but only when they are the whole role. As a substring they are
# something else entirely: "Risk Management Associate/Assistant Manager" at Shinhan Bank is
# an experienced risk hire, and "Project Management Associate" is a PMO role. Anchoring to
# the start of the title separates the two — decorative emoji and bracketed prefixes are
# skipped, so "🌟 Management Trainee" and "[Gov Healthcare] Management Associate" still
# count.
ANCHORED_PROGRAMME = re.compile(
    r"^[\W\d_]*(?:\[[^\]]*\]\s*|\([^)]*\)\s*)*"
    r"(management (associate|trainee)|graduate (management )?(associate|trainee))\b",
    re.I,
)

# Prose that states the role is open to people leaving university.
STATED_FRESH_GRAD = re.compile(
    # The plural and the clipped form both matter: "Fresh Grads only" is how the postings
    # actually write it, and a pattern expecting "fresh graduate" misses every one of them.
    r"\b(fresh grad(uate)?s?|recent grad(uate)?s?|new grad(uate)?s?|final[- ]year|"
    r"penultimate|graduating in 20\d\d|no (prior|previous|relevant) (work )?experience|"
    r"no experience (required|needed)|0\s*[-–to]{1,3}\s*[12]\s*years?|entry[ -]?level)\b",
    re.I,
)

# A fresh-graduate phrase can appear in a description in order to send graduates *away*.
# Jump Trading's "Quantitative Researcher | Trading Team" is an experienced-hire role whose
# body reads "if you are currently a student or recent graduate, please see our campus
# postings which offer both intern and full-time opportunities". Reading "recent graduate"
# there and concluding the role is open to graduates inverts the sentence's meaning.
#
# Checked in a window around each occurrence, so one redirect clause does not disqualify a
# description that genuinely invites graduates elsewhere in the text.
FRESH_GRAD_REDIRECT = re.compile(
    r"\b(?:see|refer to|visit|check|browse|explore|consider|apply (?:to|through|via))\b"
    r"[^.]{0,40}?\b(?:campus|university|student|graduate|early[ -]?career|intern)\b"
    r"[^.]{0,20}?\b(?:postings?|programmes?|programs?|opportunities|roles?|openings?|page|site)\b"
    r"|\bnot (?:intended|suitable|designed|open|available) for\b"
    r"|\bthis (?:role|position|opening|posting) is not\b",
    re.I,
)
REDIRECT_WINDOW_BEFORE = 130
REDIRECT_WINDOW_AFTER = 160

# Title-level entry markers. Weaker than a programme name, so this route additionally
# requires that nothing contradicts it on experience.
ENTRY_TITLE = re.compile(r"\b(entry[ -]?level|junior|trainee|graduate)\b", re.I)

# Structured seniority as the *platform* records it, distinct from MyCareersFuture's
# positionLevels. SmartRecruiters reports `experienceLevel` ("Entry Level", "Associate",
# "Mid-Senior Level", "Executive") and Workable reports `experience`. Both were captured into
# `extra` from the start and neither was read, so a SmartRecruiters posting explicitly tagged
# "Entry Level" was judged on its prose alone.
#
# This is the employer's own structured claim about seniority, which is the same class of
# evidence as `minimumYearsExperience == 0` — so it gets a route of its own rather than a
# weight.
EXPERIENCE_LEVEL_ENTRY = re.compile(
    r"^\s*(entry[ -]?level|graduate|student|intern(ship)?|apprentice|"
    r"trainee|junior|new grad(uate)?|no experience)\b",
    re.I,
)
EXPERIENCE_LEVEL_SENIOR = re.compile(
    r"\b(mid[- ]?senior|senior|director|executive|principal|lead|manager|"
    r"vice president|head)\b",
    re.I,
)

# ---------------------------------------------------------------------------
# Confidence scoring — ranking only, never the decision
# ---------------------------------------------------------------------------

SCORE_SIGNALS: tuple[tuple[re.Pattern[str], float, str], ...] = (
    (re.compile(r"\bgraduate\b", re.I), 3.0, "title:graduate"),
    (re.compile(r"\b(new grad|newgrad|fresh grad(uate)?)\b", re.I), 3.5, "title:new-grad"),
    (
        re.compile(r"\b(management associate|management trainee|graduate programme)\b", re.I),
        3.0,
        "title:programme",
    ),
    (re.compile(r"\b(campus|early career(s)?|apprentice(ship)?)\b", re.I), 2.5, "title:campus"),
    (
        re.compile(r"\b20\d\d\s*(start|intake|cohort)\b|\bclass of 20\d\d\b", re.I),
        2.0,
        "title:intake-year",
    ),
    (re.compile(r"\bentry[ -]?level\b", re.I), 2.0, "title:entry-level"),
    (re.compile(r"\bjunior\b", re.I), 1.0, "title:junior"),
    (re.compile(r"\btrainee\b", re.I), 1.5, "title:trainee"),
)

# Nothing here is decisive: ByteDance's genuine graduate engineering roles are all tagged
# "Professional", so treating anything other than "Fresh/entry level" as disqualifying would
# drop the largest graduate employer in the sample. The negative weights exist for the
# staffing-agency "MANAGEMENT ASSOCIATE" cluster, tagged "Senior Management" at two years.
POSITION_LEVEL_WEIGHTS: dict[str, float] = {
    "fresh/entry level": 2.0,
    "junior executive": 1.0,
    "non-executive": 0.5,
    "professional": 0.0,
    "executive": 0.0,
    "senior executive": -1.0,
    "manager": -1.5,
    "middle management": -2.0,
    "senior management": -2.0,
}

# Leonteq's "Graduate, Trading" and Merck's "GOglobal Graduate Program" are both tagged 1,
# so one year must stay mildly positive.
YEARS_WEIGHTS: dict[int, float] = {0: 2.0, 1: 1.0, 2: -1.0}

# Internship markers. "Trainee" is deliberately absent — in Singapore "Management Trainee"
# is a graduate programme, not an internship. "Traineeship" is present because SGUnited-style
# traineeships are fixed-term placements. Deloitte's "Amplify Program Audit & Assurance
# Graduate Intern" is why the title alone is not enough and employmentTypes is also read.
INTERNSHIP_TITLE = re.compile(
    r"\b(intern|interns|internship|attachment|traineeship|co[- ]?op|placement year|"
    r"industrial placement|vacation scheme|summer analyst|off[- ]?cycle)\b",
    re.I,
)


@dataclass(frozen=True)
class GradVerdict:
    """Whether a posting is graduate-level, and the evidence for it.

    ``basis`` names the route that accepted it or the veto that rejected it, and is written
    to the curated table so the dashboard can show why a row is present.
    """

    is_grad: bool
    is_internship: bool
    confidence: float
    basis: str = ""
    signals: tuple[str, ...] = field(default_factory=tuple)


def _has_junior_alternative(title: str) -> bool:
    """True when the title offers two rungs and one of them carries no seniority word.

    Employers routinely advertise a band in one posting: "Corporate Secretarial Associate /
    Senior Associate", "Senior / Community Care Associate", "Software Engineer/ Senior".
    Vetoing the whole row on the senior half throws away a role whose junior half is exactly
    what we are looking for. Three of thirty sampled seniority-vetoed rows were this shape.

    The caller still requires a structured claim of at most one year, so this cannot rescue
    a genuinely senior band.
    """
    segments = [seg for seg in re.split(r"[/|]|\bor\b", title) if seg.strip()]
    if len(segments) < 2:
        return False
    return any(not SENIORITY.search(seg) for seg in segments)


def _structured_contradiction(min_years: int | None, position_levels: tuple[str, ...]) -> bool:
    """Two independent structured signals both saying this is not an entry-level role.

    A programme name in the title normally settles it, but the sample contains a large
    cluster of staffing-agency and small-business postings titled exactly "MANAGEMENT
    ASSOCIATE" — MORE YOGURT PREMIUM, TOTAL MANPOWER, FOCUS MANPOWER, DAY ONE — every one of
    them tagged two years' experience *and* position level "Senior Management". One of those
    alone is noise. Both together are the employer telling us twice, in structured fields,
    that the programme word is not doing what it looks like.
    """
    if min_years is None or min_years < 2:
        return False
    return any(
        POSITION_LEVEL_WEIGHTS.get(level.strip().lower(), 0.0) <= -1.0 for level in position_levels
    )


def _states_fresh_grad(title: str, description: str) -> bool:
    """Whether the posting invites graduates, as opposed to merely mentioning them.

    A phrase in the title is taken at face value: "Fresh Grads only" is not ambiguous. In the
    body, each occurrence is checked against the text around it, and one sitting inside a
    "please see our campus postings" clause is pointing elsewhere, not describing this role.
    """
    if STATED_FRESH_GRAD.search(title):
        return True
    for match in STATED_FRESH_GRAD.finditer(description):
        window = description[
            max(0, match.start() - REDIRECT_WINDOW_BEFORE) : match.end() + REDIRECT_WINDOW_AFTER
        ]
        if not FRESH_GRAD_REDIRECT.search(window):
            return True
    return False


def _years_demanded_in_text(text: str) -> int | None:
    """Largest explicit years-of-experience demand in prose, if any.

    Takes the maximum rather than the first because postings routinely say "0-2 years" in
    the summary and "3 years" in the requirements; the stricter number is the real one.
    """
    found = [
        int(m.group(1))
        for m in YEARS_DEMANDED.finditer(text)
        # Look back far enough to catch "building on more than " before the number.
        if not YEARS_AS_HISTORY.search(text[max(0, m.start() - 24) : m.start()])
    ]
    return max(found) if found else None


def classify_grad(
    title: str,
    description: str = "",
    *,
    min_years: int | None = None,
    position_levels: tuple[str, ...] = (),
    employment_types: tuple[str, ...] = (),
    experience_level: str = "",
) -> GradVerdict:
    """Decide whether a posting is a graduate-level opening.

    Args:
        title: the posting title, raw.
        description: the posting body. HTML is fine; it is stripped here.
        min_years: MyCareersFuture's ``minimumYearsExperience``. None for ATS sources,
            which do not expose it.
        position_levels: MyCareersFuture's ``positionLevels`` values.
        employment_types: MyCareersFuture's ``employmentTypes`` values. Read only to catch
            internships the title does not disclose.
        experience_level: the platform's own seniority label — SmartRecruiters'
            ``experienceLevel``, Workable's ``experience``. Empty for platforms that publish
            none.

    Returns:
        A :class:`GradVerdict`. ``is_internship`` is set independently of ``is_grad`` — an
        internship is still classified and stored, just hidden by default.
    """
    title_l = title.strip()
    desc = clean_text(description)

    is_internship = bool(INTERNSHIP_TITLE.search(title_l)) or any(
        "internship" in t.lower() or "attachment" in t.lower() for t in employment_types
    )

    has_programme = bool(PROGRAMME_TITLE.search(title_l)) or bool(
        ANCHORED_PROGRAMME.search(title_l)
    )
    states_fresh = _states_fresh_grad(title_l, desc)

    # --- vetoes -----------------------------------------------------------
    if NOT_A_ROLE.search(title_l):
        return GradVerdict(False, is_internship, 0.0, "veto:not-a-role")
    if SUBJECT_MATTER_GRADUATE.search(title_l):
        return GradVerdict(False, is_internship, 0.0, "veto:graduate-as-subject-matter")
    if ACADEMIC_TRACK.search(title_l):
        return GradVerdict(False, is_internship, 0.0, "veto:academic-track")

    level = (experience_level or "").strip()
    level_is_entry = bool(level) and bool(EXPERIENCE_LEVEL_ENTRY.match(level))
    # A platform saying "Mid-Senior Level" outright is at least as good as a title keyword,
    # and unlike a title it cannot be marketing. It does not override a named programme,
    # though: a firm can tag its graduate scheme carelessly.
    if level and not level_is_entry and EXPERIENCE_LEVEL_SENIOR.search(level) and not has_programme:
        return GradVerdict(False, is_internship, 0.0, f"veto:experience-level-{level.lower()}")

    effective_years = min_years if min_years is not None else _years_demanded_in_text(desc)
    if effective_years is not None and effective_years >= YEARS_VETO:
        return GradVerdict(False, is_internship, 0.0, f"veto:{effective_years}-years-required")

    if SENIORITY.search(title_l):
        # Two exceptions, both narrow, both requiring a structured claim of at most one
        # year so the rescue can never fire on prose alone.
        #
        # Charles & Keith posts "MANAGEMENT ASSOCIATE (TRAINEE MANAGER)" at zero years — a
        # real graduate programme whose title happens to contain "Manager". And "#2 Ola
        # Trainee - AI Product Manager (Fresh Grads only)" at zero years says outright who
        # it is for, then loses to the word "Manager".
        #
        # Without the structured field we do not gamble: "Senior Engineer/ Engineer" at
        # ams-OSRAM and "Senior Data Analyst" both stay rejected.
        rescuable = has_programme or states_fresh or _has_junior_alternative(title_l)
        rescued = rescuable and min_years is not None and min_years <= 1
        if not rescued:
            return GradVerdict(False, is_internship, 0.0, "veto:seniority-in-title")

    # --- confidence (ranking only) ----------------------------------------
    score = 0.0
    signals: list[str] = []
    for pattern, weight, name in SCORE_SIGNALS:
        if pattern.search(title_l):
            score += weight
            signals.append(name)
    if min_years is not None:
        score += YEARS_WEIGHTS.get(min_years, 0.0)
        signals.append(f"years:{min_years}")
    for level in position_levels:
        weight = POSITION_LEVEL_WEIGHTS.get(level.strip().lower())
        if weight is not None:
            score += weight
            signals.append(f"level:{level.strip().lower()}")

    # --- routes -----------------------------------------------------------
    # Ordered by how much the reason is worth stating. The first that fires is the basis.
    if has_programme and not _structured_contradiction(min_years, position_levels):
        return GradVerdict(True, is_internship, round(score, 2), "route:programme", tuple(signals))

    if states_fresh:
        return GradVerdict(
            True, is_internship, round(score, 2), "route:states-fresh-grad", tuple(signals)
        )

    # The user's third leg: entry-level roles with zero years in the required range. This is
    # a structured claim by the employer, not an inference, so it stands on its own.
    if min_years == 0:
        return GradVerdict(True, is_internship, round(score, 2), "route:zero-years", tuple(signals))

    # Same class of evidence, different platform. SmartRecruiters tagging a posting
    # "Entry Level" is the employer stating the seniority in a structured field.
    if level_is_entry:
        return GradVerdict(
            True, is_internship, round(score, 2), "route:experience-level", tuple(signals)
        )

    # A weak title marker ("Junior Quant Researcher" at Squarepoint) counts only when
    # experience does not contradict it.
    if ENTRY_TITLE.search(title_l) and (min_years is None or min_years <= 1):
        return GradVerdict(
            True, is_internship, round(score, 2), "route:entry-level-title", tuple(signals)
        )

    return GradVerdict(False, is_internship, round(score, 2), "no-route", tuple(signals))


# ---------------------------------------------------------------------------
# Job family
# ---------------------------------------------------------------------------

# Ordered most-specific to least-specific; first match wins. Order is the whole design here,
# so changing it is a behavioural change and needs the golden set re-run.
#
# Orderings that look wrong and are not:
#   * Supply Chain precedes Operations, or "Supply Chain Operations Analyst" lands generic.
#   * Data Science precedes Software Engineering, or "Machine Learning Engineer" is filed as
#     a generic engineer. "Data Engineer" is deliberately Software Engineering: it is an
#     infrastructure role, and grouping it with analysts would misrepresent the work.
#   * Software Engineering precedes Engineering, or every SWE title matches "engineer" first.
#   * General Management is last before OTHER. It is the correct answer for a rotational
#     programme that names no discipline ("Graduate Management Associate"), but it would
#     swallow half the board if it ran earlier.
FAMILY_RULES: tuple[tuple[JobFamily, re.Pattern[str]], ...] = (
    (
        JobFamily.QUANT_TRADING,
        # Bare "trading" is included so Leonteq's "Graduate, Trading" lands somewhere useful,
        # but only when it is not the back-office sense — "Trading Operations" and "Trade
        # Support" are Operations roles and must not be filed on a trading desk.
        re.compile(
            r"\b(quant(itative)?[ -](trader|trading)|trader|trading (analyst|associate|desk)|"
            r"market mak(er|ing)|execution trad|prop(rietary)? trad|"
            r"trading(?!\s+(operations|support|settlement|ops|controls?)))\b",
            re.I,
        ),
    ),
    (
        JobFamily.QUANT_RESEARCH,
        re.compile(
            r"\b(quant(itative)?[ -]research|quant(itative)? researcher|"
            r"systematic research|quant(itative)? strateg)\b",
            re.I,
        ),
    ),
    (
        JobFamily.QUANT_DEV,
        re.compile(
            r"\b(quant(itative)?[ -](developer|dev|engineer|technolog)|"
            r"quant(itative)? analyst|quant)\b",
            re.I,
        ),
    ),
    (
        JobFamily.STRATEGY_OPERATIONS,
        re.compile(
            r"\bstrategy (and|&) (operations|ops)\b|\bbiz ?ops\b|\bstrat(egy)? ?ops\b", re.I
        ),
    ),
    (
        JobFamily.STRATEGY_CONSULTING,
        re.compile(
            r"\b(strategy consult|strategic consult|parthenon|strategy (and|&) execution|"
            r"corporate strategy|commercial strategy|strategy (analyst|associate|consultant)|"
            r"transaction (advisory|services)|deal advisory|due diligence)\b",
            re.I,
        ),
    ),
    (
        JobFamily.MANAGEMENT_CONSULTING,
        re.compile(
            r"\b(management consult|business consult|consulting (analyst|associate|graduate)|"
            r"associate consultant|consultant|advisory (analyst|associate)|business advisory)\b",
            re.I,
        ),
    ),
    (
        JobFamily.SUPPLY_CHAIN,
        re.compile(
            r"\b(supply chain|procurement|logistics|sourcing|demand planning|supply planning|"
            r"inventory|warehouse|fulfil?lment|freight|shipping|materials management)\b",
            re.I,
        ),
    ),
    (
        JobFamily.PRODUCT_MANAGEMENT,
        re.compile(r"\b(product manager|product management|product owner|apm)\b", re.I),
    ),
    (
        JobFamily.DATA_SCIENCE,
        re.compile(
            # "Data & AI" is how Merck names the data track of its GOglobal programme; with
            # only `data scien` here the row fell through to General Management.
            r"\b(data scien(ce|tist)|machine learning|deep learning|ml|mlops|"
            r"algorithm engineer|applied scientist|research scientist|"
            r"ai (engineer|scientist|algorithm)|data (&|and) ai|nlp|computer vision|mllm)\b",
            re.I,
        ),
    ),
    (
        JobFamily.DATA_ANALYST,
        re.compile(
            r"\b(data analyst|data analytics|business intelligence|bi (analyst|developer)|"
            r"reporting analyst|insights analyst|analytics (associate|graduate))\b",
            re.I,
        ),
    ),
    (
        JobFamily.BUSINESS_ANALYST,
        re.compile(
            r"\b(business analyst|product analyst|process analyst|systems analyst|"
            r"business systems)\b",
            re.I,
        ),
    ),
    (
        JobFamily.SOFTWARE_ENGINEERING,
        re.compile(
            r"\b(software engineer|software develop|swe|back[- ]?end|front[- ]?end|"
            r"full[- ]?stack|web develop|mobile (engineer|develop)|android|ios|"
            r"devops|site reliability|sre|platform engineer|infrastructure engineer|"
            r"cloud engineer|security engineer|systems engineer|embedded|firmware|"
            r"data engineer|big data engineer|solutions? architect|developer|programmer|"
            r"qa engineer|test engineer|automation engineer)\b",
            re.I,
        ),
    ),
    (
        JobFamily.INVESTMENT,
        re.compile(
            r"\b(investment (analyst|associate|banking|management|professional)|"
            r"equity research|portfolio manage|asset management|private equity|"
            r"venture capital|ibd|corporate finance|mergers|m&a|capital markets|"
            r"wealth manage|fund manage)\b",
            re.I,
        ),
    ),
    (
        JobFamily.RISK_COMPLIANCE,
        re.compile(
            r"\b(risk (analyst|management|associate|officer)|credit risk|market risk|"
            r"operational risk|compliance|aml|kyc|financial crime|regulatory|"
            r"internal audit|audit (analyst|associate|graduate)|assurance)\b",
            re.I,
        ),
    ),
    (
        JobFamily.HUMAN_RESOURCES,
        re.compile(
            r"\b(human resources|hr (analyst|associate|graduate|executive|generalist)|"
            r"talent acquisition|people (operations|analyst|partner)|recruitment|"
            r"compensation (and|&) benefits|learning (and|&) development)\b",
            re.I,
        ),
    ),
    (
        JobFamily.SALES_MARKETING,
        # Bare "growth" and "communications" were here and had to go: they matched "Growth
        # Engineer" and "Communications Engineer" ahead of the engineering rules. Both now
        # require a commercial noun beside them.
        re.compile(
            r"\b(sales|marketing|brand manage|account (executive|manage)|"
            r"business development|growth (marketing|manager|associate|analyst|strateg)|"
            r"customer success|category manage|merchandis|public relations|"
            r"corporate communications)\b",
            re.I,
        ),
    ),
    (
        JobFamily.LEGAL,
        re.compile(r"\b(legal|counsel|paralegal|contracts? (analyst|manage))\b", re.I),
    ),
    (
        JobFamily.ENGINEERING,
        re.compile(
            r"\b(mechanical|electrical|electronics|civil|structural|chemical|process|"
            r"manufacturing|industrial|production|quality|reliability|field service|"
            r"application|design|test|equipment|maintenance|facilit(y|ies)|"
            r"r&d|research and development)\b[^.]{0,20}\bengineer",
            re.I,
        ),
    ),
    (
        JobFamily.OPERATIONS,
        re.compile(
            r"\b(operations|operational|ops|service delivery|process improvement|"
            r"operational excellence)\b",
            re.I,
        ),
    ),
    (
        JobFamily.FINANCE_ACCOUNTING,
        # The bare `finance|financial` alternatives are last-resort but necessary: "Finance
        # Specialist" matched no title rule at all and fell through to the description
        # fallback, which filed it under Risk & Compliance on an incidental mention of
        # "compliance" in the job body. Catching it on the title is strictly better than
        # letting the fallback guess.
        re.compile(
            r"\b(accountant|accounting|treasury|taxation|tax|controller|fp&a|"
            r"finance|financial)\b",
            re.I,
        ),
    ),
    # Deliberately last. A rotational graduate programme that names no discipline is General
    # Management, not Other — but only once every specific rule has declined it.
    (
        JobFamily.GENERAL_MANAGEMENT,
        re.compile(
            # The trailing `s?` matters: "Graduate Programmes" (plural, as CTES writes it)
            # failed `\bprogramme\b` outright and fell through to Other.
            r"\b(management associate|management trainee|graduate (management|trainee|"
            r"program(me)?s?|associate|engineer|opportunit)|rotational|"
            r"leadership program(me)?s?|general management)\b",
            re.I,
        ),
    ),
    # A bare "Engineer" with no discipline in front of it. Runs after General Management so
    # "Graduate Engineering Trainee" is read as a programme first.
    (JobFamily.ENGINEERING, re.compile(r"\bengineer(ing)?\b", re.I)),
)


@dataclass(frozen=True)
class FamilyVerdict:
    """The assigned family, its group, and where the decision came from."""

    family: JobFamily
    group: str
    confidence: float
    basis: str


def classify_family(title: str, description: str = "", department: str = "") -> FamilyVerdict:
    """Assign a job family.

    Matching runs against the title first, then the department, then the description. A
    title match is worth far more than a description match: descriptions mention adjacent
    disciplines constantly ("you will work with our data science team"), and matching on
    that files half the board under Data Science.
    """
    title_l = title.strip()

    for family, pattern in FAMILY_RULES:
        if pattern.search(title_l):
            return FamilyVerdict(family, FAMILY_GROUPS[family], 0.9, "title")

    dept = department.strip()
    if dept:
        for family, pattern in FAMILY_RULES:
            if pattern.search(dept):
                return FamilyVerdict(family, FAMILY_GROUPS[family], 0.6, "department")

    # Only the first 600 characters. Past that a description is boilerplate about benefits
    # and the company mission, and matches there are noise.
    desc = clean_text(description)[:600]
    if desc:
        for family, pattern in FAMILY_RULES:
            if pattern.search(desc):
                return FamilyVerdict(family, FAMILY_GROUPS[family], 0.4, "description")

    return FamilyVerdict(JobFamily.OTHER, FAMILY_GROUPS[JobFamily.OTHER], 0.0, "unmatched")
