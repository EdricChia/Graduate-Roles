# Taxonomy

How a posting gets classified. The rules live in `src/gradtrack/transform/classify.py`; this
file explains what they mean and why they are shaped the way they are. Both classifiers are pure
functions and every change is measured against `data/manual/grad_labels.csv`.

Everything below was derived from a 2,979-posting sample captured from MyCareersFuture on
2026-08-08, plus the Jane Street (230 postings) and Coinbase (174) Greenhouse boards.

---

## Graduate eligibility

Scope has three distinct legs, so eligibility is decided by **named routes**, not by a score
crossing a threshold. Each acceptance states its reason, and that reason is written to
`grad_basis` and shown in the dashboard.

An earlier version scored everything into one number and accepted "Retail Associate" and "Front
Office Associate (Hotel)" on nothing but `minimumYearsExperience: 1` plus position level
`Fresh/entry level` — two weak structured signals, no evidence, exactly at the threshold. Routes
make that impossible to express.

### Vetoes, checked first

| Veto | Kills | Example |
|---|---|---|
| `graduate-as-subject-matter` | "graduate" describing the work, not the hire | NUS, "Executive, Graduate Studies' Office" |
| `academic-track` | postdocs and faculty | NTU, "Research Fellow (Power Grids and Markets)" |
| `{n}-years-required` | 3+ years, from the structured field or from prose | Asahi, "Go Graduate Finance" (3 years) |
| `seniority-in-title` | senior / lead / principal / manager / director / VP … | "Senior Data Analyst #ESY" |

Word boundaries carry real weight in the seniority veto: `\bmanager\b` must not match
"Management Associate" or "Management Trainee", which are the standard Singapore names for a
graduate programme.

**The seniority veto has three narrow escapes**, each additionally requiring a structured claim
of at most one year, so none can fire on prose alone:

1. A named programme — Charles & Keith's "MANAGEMENT ASSOCIATE (TRAINEE MANAGER)" at 0 years.
2. An explicit fresh-graduate statement — "#2 Ola Trainee - AI Product Manager (Fresh Grads
   only)" at 0 years.
3. A dual-rung title where one rung carries no seniority word — "Corporate Secretarial Associate
   / Senior Associate", "Senior Engineer/ Engineer". Three of thirty sampled seniority-vetoed
   rows were this shape, so it is a pattern rather than a one-off.

### Routes, in order

| Route | Fires when | Notes |
|---|---|---|
| `programme` | title names a graduate programme | Blocked by a *structured contradiction*: 2+ years **and** a managerial position level. This is what removes the staffing-agency "MANAGEMENT ASSOCIATE" cluster (MORE YOGURT PREMIUM, TOTAL MANPOWER, FOCUS MANPOWER), all tagged 2 years and "Senior Management". |
| `states-fresh-grad` | title or body says fresh grad / final year / no experience required | Matches the plural: postings write "Fresh Grads only", not "fresh graduate". |
| `zero-years` | `minimumYearsExperience == 0` | Scope leg three. A structured claim by the employer, so it stands alone. |
| `entry-level-title` | title says entry-level / junior / trainee, and experience does not contradict | Catches "Junior Quant Researcher" at Squarepoint. |

"Management Associate" and "Management Trainee" only count as programme names when **anchored to
the start of the title**. As a substring they mean something else: Shinhan Bank's "Risk
Management Associate/Assistant Manager" is an experienced risk hire, not a graduate programme.
Decorative prefixes are skipped, so "🌟 Management Trainee" and "[Gov Healthcare] Management
Associate" still anchor.

### What is deliberately *not* a signal

**Position level is a weight, never a gate.** All ~25 of ByteDance's Singapore graduate
engineering roles are tagged `Professional`, not `Fresh/entry level`. Filtering on entry level
would drop the largest graduate employer in the sample.

**`minimumYearsExperience == 0` is not required.** Leonteq's "Graduate, Trading" and Merck's
"GOglobal Graduate Program" are both tagged `1`.

### Internships

Flagged, stored, and hidden from the default view — never dropped, so re-including them stays a
filter change. Detected from the title *and* from `employmentTypes`: Deloitte's "Amplify Program
Audit & Assurance Graduate Intern" is graduate-titled and an internship at once.

"Trainee" is deliberately **not** an internship marker. In Singapore "Management Trainee" is a
graduate programme. "Traineeship" is, because SGUnited-style traineeships are fixed-term.

---

## Job families

Ordered rules, first match wins, applied to title → department → first 600 characters of the
description. A title match scores 0.9, department 0.6, description 0.4. Descriptions mention
adjacent disciplines constantly ("you will work with our data science team"), so matching on
them at full weight would file half the board under Data Science.

| Family | Group |
|---|---|
| Strategy Consulting · Management Consulting · Strategy & Operations | **Strategy & Consulting** ★ |
| Quant Trading · Quant Research · Quant Dev | **Quant & Trading** ★ |
| Data Science · Data Analyst · Business Analyst | **Data & Analytics** ★ |
| Software Engineering | **SWE & Technical** ★ |
| Operations | **Operations** ★ |
| Supply Chain | **Supply Chain** ★ |
| Investment | Investment |
| Product Management | Product |
| Risk & Compliance | Risk & Compliance |
| Finance & Accounting | Finance |
| Engineering | Engineering |
| Sales & Marketing | Sales & Marketing |
| Human Resources | People |
| Legal | Legal |
| General Management | General Management |
| Other | Other |

★ = pushed to Telegram. Everything else is still tracked and still visible in the dashboard.

### Orderings that look wrong and are not

- **Supply Chain before Operations** — otherwise "Supply Chain Operations Analyst" lands generic.
- **Data Science before Software Engineering** — otherwise "Machine Learning Engineer" is filed as
  a generic engineer.
- **Data Engineer is Software Engineering, not Data** — it is an infrastructure role, and grouping
  it with analysts misrepresents the work.
- **Software Engineering before Engineering** — otherwise every SWE title matches "engineer" first.
- **General Management last but one** — it is the right answer for a rotational programme naming
  no discipline ("Graduate Management Associate", Merck's "GOglobal Graduate Program"), and the
  wrong answer for anything that a specific rule would have claimed. Adding it cut unclassified
  graduate rows from 345 to 63.
- **Bare `trading` excludes the back office** — "Trading Operations" and "Trade Support" are
  Operations roles, so the pattern carries a negative lookahead.

### Corrections

Individual mistakes go in `data/manual/family_overrides.csv`, keyed by `job_key` — not into the
rule table. A rule exists to generalise; a one-off fix that generalises badly is worse than no
rule. If the same override appears three times for the same reason, write the rule then.

---

## Measuring changes

`uv run pytest tests/test_classify.py` prints precision, recall and family accuracy against the
golden set on every run and fails below the thresholds in that file.

Read the header of `tests/test_classify.py` before trusting the headline numbers. Most golden
rows were seeded from classifier output and reviewed, which makes them a **regression lock rather
than an unbiased accuracy estimate**. The independent evidence is the twenty hand-written hard
cases and the named traps. A fresh blind-labelled sample is worth building once the ATS legs are
wired and the pool is no longer MyCareersFuture-only.

## Known gaps

- **ATS rows have no structured experience field.** A posting at a target firm that never says
  "graduate" is classified as not-graduate — every Jane Street Singapore role currently falls
  here. That is right for precision, but it means the tracker will not surface an
  otherwise-eligible role that simply does not advertise itself as one. A "all roles at tracked
  firms" secondary view is the intended answer, not a looser classifier.
- **MyCareersFuture needs firm gating, not just role classification.** The pool is full of
  staffing agencies and small businesses posting genuine entry-level roles. "MANAGEMENT ASSOCIATE
  at MORE YOGURT PREMIUM" is correctly graduate-level and still not something to track. That is
  the registry's job, not the classifier's — the two must not be conflated.
