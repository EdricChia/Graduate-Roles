# Build spec

Phases are ordered by **descending durability**: the most reliable source is wired first and the
most fragile last, so a broken scraper can never block the parts that work. Do not start a phase
until the previous phase's acceptance criteria pass.

Phase 6 was brought forward ahead of Phases 4 and 5. The durability ordering is about *sources*,
and Phase 6 adds none — it makes the pipeline usable end to end, which is worth having before the
fragile platforms are wired rather than after.

---

## Phase 0 — scaffolding ✅

`uv` project, `ruff`, `pytest`, `config.py`, `schema.py`, `firms.py`, the firm registry, the
`.claude` rules.

**Acceptance met:** `uv run pytest` green; `load_registry` validates the committed registry.

---

## Phase 1 — schema and classifiers ✅

Written against 2,979 live MyCareersFuture postings plus the Jane Street and Coinbase Greenhouse
boards, captured as fixtures before a line of rule code.

- `transform/classify.py` — route-based graduate eligibility and an ordered family rule list.
- `data/manual/grad_labels.csv` — 324 labelled rows, 20 of them hand-written hard cases.
- `tests/test_classify.py` — precision, recall and family accuracy, gated in CI.

**Acceptance met:** precision 1.000, recall 0.994, family accuracy 0.997. Read the test header
before trusting those numbers — they are a regression lock, not an unbiased estimate.

---

## Phase 2 — MyCareersFuture ✅

- `sources/base.py` — rate limiting, retries, and `FetchOutcome`, which is what makes
  "absence is not closure" possible.
- `sources/mcf.py`, `ingest/mcf.py`, `ingest/snapshot.py`.

**Acceptance met:** 297 postings in a partial sweep; `originalPostingDate` used, not
`newPostingDate`.

---

## Phase 3 — the clean JSON ATS platforms ✅

Greenhouse, Lever, Ashby, SmartRecruiters, Workable — five documented, unauthenticated GET APIs —
plus `discover.py`, which probes public boards to identify a firm's ATS.

**Acceptance met:** 14 Jane Street and 6 Coinbase Singapore roles with company-domain apply links.
Discovery verification is covered by `tests/test_discover.py`, built from four real
misattributions found in live runs.

---

## Phase 4 — the hard ATS platforms 🔶

**Workday** is wired and verified against four live tenants (NVIDIA, Salesforce, DBS, Micron).
Everything measured about it is in `SOURCES.md`; the load-bearing facts are that `limit` above 20
returns an empty array, that `searchText` is the only affordable way to narrow a 2,700-posting
tenant, and that multi-location rows hide their locations behind "N Locations".

**SuccessFactors** is wired and verified against Temasek (42 Singapore postings). It was expected
to need Phase 5 and does not — see below.

**Still to do:** Oracle Recruiting Cloud, Eightfold, Phenom. None has a confirmed target firm yet;
they should be built when discovery identifies one, not before.

---

## Phase 5 — Playwright fallback ⬜ deliberately not built

Scope was "strict robots.txt plus a headless browser for JS-only career sites". The hook is in
place — `Platform.BROWSER` exists in the schema, the registry validates a `browser` row as needing
a `careers_url`, and `pyproject.toml` carries an optional `browser` extra — but no client is
written, on purpose.

**No firm has been shown to need one.** The candidate that motivated the phase was Temasek, whose
SuccessFactors site turned out to be server-rendered and robots-permitted. Every source wired so
far is JSON or server-rendered HTML.

Writing an untested browser client now would add a ~400MB dependency, a second CI workflow and the
project's most fragile code path, in exchange for zero demonstrated coverage. The honest sequence
is the other way round: finish Workday, Oracle, Eightfold and Phenom discovery, see which Tier-1
firms are still unreachable, check their `robots.txt`, and build the fallback against a real
target.

**Acceptance, when it is built:** remaining Tier-1 firms either covered or explicitly recorded in
`SOURCES.md` as `unavailable`.

---

## Phase 6 — lifecycle, dashboard, notifier, automation ✅

- `transform/lifecycle.py` — snapshot diffing with the absence-is-not-closure guard.
- `transform/dedupe.py` — MyCareersFuture employers resolved onto registry firms; unmatched ones
  become discovery candidates rather than noise or silence.
- `transform/build.py` — the curated table.
- `app.py`, `notify/telegram.py`, `refresh_check.py`, `.github/workflows/refresh.yml`.

**Acceptance met:** `tests/test_lifecycle.py` proves a failed fetch leaves postings `unknown`
rather than `closed`, that one firm's outage does not touch another's, and that a
Greenhouse-only day does not close every Workday posting.

---

## What is left

1. **Registry coverage.** The remaining `todo` firms are mostly on Workday, SuccessFactors and
   Oracle — the platforms whose tokens cannot be guessed. Work through them with the `add-firm`
   skill, using the discovery-candidates report to prioritise.
2. **Phase 4 platforms** beyond Workday.
3. **Phase 5** browser fallback.
4. **A blind-labelled classifier sample**, once the pool is no longer MyCareersFuture-dominated.
   The current golden numbers are a regression lock and should not be read as accuracy.
