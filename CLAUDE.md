# gradtrack

## Purpose

Track every graduate-level opening in Singapore at large, well-paying, prestigious employers —
banks, quant firms, big tech, crypto, MBB and the wider consulting field, sovereign entities like
GIC and Temasek, and the MNCs. For each one: what it is, which job family it belongs to, when it
went up, whether it is still open, and a link to the firm's own careers page.

**The link is the product.** Firms publish to their own ATS first and syndicate to
MyCareersFuture and LinkedIn days later, because board posting is driven by Fair Consideration
Framework compliance timing rather than recruiting urgency. A tracker built on aggregators tells
you about roles after everyone else has applied. So this reads company career sites directly, and
`apply_url` always resolves to the firm's own domain.

## Repo layout

```
gradtrack/
  pyproject.toml
  config.toml                    # user agent / contact email / telegram, gitignored
  CLAUDE.md
  SOURCES.md                     # one entry per platform + every firm we probed and refused
  DATA_CONTRACT.md               # written at the end of Phase 2, not before
  TAXONOMY.md                    # job family definitions and the ordered classification rules
  BUILD_SPEC.md                  # the phased build plan this repo follows
  .claude/
    rules/  skills/
  src/gradtrack/
    config.py                    # config.toml loader
    schema.py                    # SourcePosting, Status, JobFamily — the data contract
    firms.py                     # the registry loader and its validation
    sources/                     # dumb clients, one per PLATFORM, never per firm
    ingest/                      # fetch -> data/raw/, no transformation
    transform/                   # raw -> curated tables
      classify.py                # pure functions: job family + graduate eligibility
      lifecycle.py               # snapshot diff -> first_seen / last_seen / status
      dedupe.py                  # MCF <-> ATS match; the company site always wins
    notify/                      # telegram + the markdown digest
    refresh_check.py             # health check as code; writes reports/health/
  data/
    firms/registry.csv           # THE core asset: firm -> platform -> token
    raw/                         # source=<platform>/snapshot_date=<date>/*.parquet
    manual/                      # hand-keyed CSVs: golden labels, overrides, aliases
    curated/                     # *.parquet
    curated.duckdb               # derived, gitignored
  tests/                         # classify + lifecycle are the ones that must always pass
  reports/coverage/  reports/health/
  app.py
  .github/workflows/refresh-fast.yml  refresh-browser.yml
```

## Commands

| Task | Command |
|---|---|
| Install / sync deps | `uv sync` |
| Run tests | `uv run pytest` |
| Ingest one firm | `uv run python -m gradtrack.ingest.ats --firm janestreet` |
| Ingest one platform | `uv run python -m gradtrack.ingest.ats --platform greenhouse` |
| Ingest MyCareersFuture | `uv run python -m gradtrack.ingest.mcf` |
| Rebuild curated tables | `uv run python -m gradtrack.transform` |
| Health check | `uv run python -m gradtrack.refresh_check` |
| Telegram dry run | `uv run python -m gradtrack.notify.telegram --dry-run` |
| Dashboard | `uv run streamlit run app.py` |
| Lint + format | `uv run ruff check --fix` and `uv run ruff format` |

## Stack

`uv` (deps), `httpx` (HTTP), `tenacity` (retries), `pydantic` (boundary validation), `polars`
(dataframes), `duckdb` (reads parquet directly, no server), `rapidfuzz` (MCF↔ATS title matching),
`streamlit` (dashboard), `ruff`, `pytest`. One optional extra: `browser` (`playwright`), used by
exactly one pipeline and installed with `uv sync --extra browser`.

## Build order

Phase 0 scaffolding → Phase 1 schema + classifiers on fixtures → Phase 2 MyCareersFuture →
Phase 3 clean JSON ATS platforms → Phase 4 hard ATS platforms → Phase 5 Playwright fallback →
Phase 6 lifecycle, dashboard, notifier, CI.

Built in descending order of durability, so the fragile leg can never block the rest. Do not start
a phase until the previous phase's acceptance criteria pass. Full detail in `BUILD_SPEC.md`.

## Design principles

These are invariants, not preferences.

1. **One client per platform, never per firm.** Adding a company is a row in
   `data/firms/registry.csv`. If you find yourself writing `sources/janestreet.py`, stop.
2. **Ingest never transforms and never classifies.** Source clients fetch, validate against
   `SourcePosting`, and write to `data/raw/` untouched. Family and grad-eligibility are decided in
   `transform/classify.py`.
3. **Everything is snapshot-dated and append-only.** `source=<platform>/snapshot_date=<YYYY-MM-DD>/`,
   never overwritten. The lifecycle table is *derived* from this history, so the history is the
   only thing that cannot be rebuilt.
4. **Absence is not closure.** A posting missing from today's snapshot is `closed` only if that
   firm's fetch succeeded and returned rows. Otherwise it is `unknown` and keeps its prior status.
   See `.claude/rules/ingest.md`.
5. **Classifier functions are pure.** No I/O inside `transform/classify.py`. It is the one part of
   this repo that is genuinely hard to get right, so it must stay trivially testable.
6. **Never invent a date.** `posted_date` carries a `posted_date_basis` saying whether the platform
   published it or we merely observed it. Missing data is null and shows up in a coverage report.
7. **Robots.txt decides, not convenience.** A disallowed board is recorded in `SOURCES.md` as
   `probed_not_used` and never fetched, however easy it would be.

## Testing

`tests/test_classify.py` and `tests/test_lifecycle.py` must always pass — they cover the two
things that fail silently rather than loudly. Do not write tests that hit live careers sites;
mark anything that must with `@pytest.mark.network`, which the default run excludes.

---

Procedures live in `.claude/skills/`. Constraints live in `.claude/rules/`. Do not duplicate them
here.
