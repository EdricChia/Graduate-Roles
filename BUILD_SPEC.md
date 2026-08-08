# Build spec

Phases are ordered by **descending durability**: the most reliable source is wired first and the
most fragile last, so a broken scraper can never block the parts that work. Do not start a phase
until the previous phase's acceptance criteria pass.

---

## Phase 0 — scaffolding ✅

`uv` project, `ruff`, `pytest`, `config.py`, `schema.py`, `firms.py`, the firm registry, the
`.claude` rules, and this file.

**Acceptance:** `uv run pytest` green; `load_registry` validates the committed registry.

---

## Phase 1 — schema and classifiers, on fixtures

The classifier is written before any ingestion, against committed JSON fixtures captured from real
payloads. No network. This is the same discipline as writing the model before the pipeline: the
classifier defines what the data has to contain.

- `transform/classify.py` — `classify_grad()` and `classify_family()`, pure functions.
- `data/manual/grad_labels.csv` — ~300 hand-labelled postings, the golden set.
- `tests/test_classify.py` — asserts precision and recall against the golden set.

**Acceptance:** precision and recall reported; `test_classify.py` passes; no rule in the table
lacks a labelled row that justifies it.

---

## Phase 2 — MyCareersFuture

Broadest, most durable, permissive `robots.txt`, and a government API with no key. Wired second
despite being a secondary source, because it is the only one that can validate the classifier at
volume before the ATS clients exist.

- `sources/mcf.py`, `ingest/mcf.py`, `transform/normalise.py` (MCF leg).
- `DATA_CONTRACT.md` written at the end of this phase, once the shape is real.

**Acceptance:** Singapore graduate postings in `data/curated/postings.parquet`;
`originalPostingDate` used for `posted_date`, not `newPostingDate`.

---

## Phase 3 — the clean JSON ATS platforms

Greenhouse, Lever, Ashby, SmartRecruiters, Workable. Five documented, unauthenticated GET APIs.
This is where the tracker starts beating the job boards.

- One client per platform in `sources/`, driven by the registry.
- Registry discovery pass: resolve `ats_platform` and `ats_token` for every Tier-1 firm on these
  platforms, flipping rows from `todo` to `wired`.

**Acceptance:** Jane Street and Coinbase Singapore roles present with `janestreet.com` /
`coinbase.com` URLs; every wired row returns rows or is demoted with a recorded reason.

---

## Phase 4 — the hard ATS platforms

Workday, SuccessFactors, Oracle Recruiting Cloud, Eightfold, Phenom. Undocumented, POST-based,
per-tenant discovery, bot management. Most banks and large MNCs live here, so the value is high and
so is the breakage rate.

**Acceptance:** the bank and MNC cohort wired; Temasek resolving through SuccessFactors; Workday
paging capped at 20 per request with backoff proven against a real tenant.

---

## Phase 5 — Playwright fallback

For careers sites that render entirely in JS and expose no feed, and whose `robots.txt` permits
crawling. Optional `browser` extra, separate CI workflow, never on the critical path.

**Acceptance:** remaining Tier-1 firms either covered or explicitly recorded in `SOURCES.md` as
`unavailable`.

---

## Phase 6 — lifecycle, dashboard, notifier, automation

- `transform/lifecycle.py` — snapshot diff to `first_seen` / `last_seen` / `status`, with the
  absence-is-not-closure guard.
- `transform/dedupe.py` — MCF↔ATS matching; the ATS row is canonical and keeps its company URL.
- `app.py` — Streamlit, default sorted by `posted_date` descending.
- `notify/telegram.py` — daily push for the six priority family groups.
- `refresh_check.py` + the two GitHub Actions workflows.

**Acceptance:** replaying two snapshots where one firm's fetch is forced to fail leaves its roles
`unknown`, not `closed`, and fires no notification.
