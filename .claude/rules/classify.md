---
paths:
  - "src/gradtrack/transform/classify.py"
  - "data/manual/grad_labels.csv"
  - "TAXONOMY.md"
---

# Classifier constraints

The classifier decides two things: is this posting graduate-level, and which job family is it. It
is the hardest correctness problem in the repo, because both failure modes are invisible. A false
negative means a role you wanted never reaches your phone. A false positive means the tracker
slowly fills with noise until you stop reading it.

- **Pure functions only.** No I/O, no network, no file reads inside `classify.py`. The rule tables
  are module-level constants; the golden set is loaded by the tests, not by the classifier.
- **Every change is measured, not argued.** Adding or editing a rule requires running the golden
  set and reporting precision and recall before and after. `uv run pytest tests/test_classify.py`
  prints both. If precision drops below the threshold, CI fails.
- **Never let a single signal decide graduate eligibility.** Live MyCareersFuture data proves why:
  Leonteq's "Graduate, Trading" and Merck's "GOglobal Graduate Program" are both tagged
  `minimumYearsExperience: 1`, ByteDance's "Backend Engineer Graduate" is `0`, and real grad
  programmes appear under three different `positionLevels` values. Meanwhile NUS's "Executive,
  Graduate Studies' Office" and a five-year "Assistant Manager, Academic" both match the word
  "graduate". Structured metadata is a weighted signal, never a decisive one.
- **Corrections go in `data/manual/family_overrides.csv`, keyed by `job_key`** — not into the rule
  table. A rule exists to generalise; a one-off fix that generalises badly is worse than no rule.
  If the same override appears three times for the same reason, *then* write the rule.
- **The golden set is append-only in spirit.** Never delete a labelled row because the classifier
  now gets it wrong. That is the row earning its keep.
- Internships are excluded from the default view but still classified and still stored, with
  `is_internship = true`. Re-including them must stay a filter change, never a re-ingest.
