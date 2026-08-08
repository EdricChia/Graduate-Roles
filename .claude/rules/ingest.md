---
paths:
  - "src/gradtrack/ingest/**"
  - "src/gradtrack/sources/**"
---

# Ingest constraints

- Ingest code writes raw responses to `data/raw/source=<platform>/snapshot_date=<YYYY-MM-DD>/`. It
  must not reshape, filter, rename, derive, or classify. If you feel the urge to decide whether a
  role is graduate-level here, that logic belongs in `transform/classify.py`.
- Raw snapshots are append-only. Never overwrite or delete an existing snapshot directory. A rerun
  on the same day writes into the same dated partition; it does not rewrite history. The lifecycle
  table is derived from this history, so the history is the one artifact that cannot be rebuilt.
- **One client per platform, never per firm.** `sources/greenhouse.py` serves every Greenhouse
  firm in the registry. A file named after a company is a design failure.
- Every outbound request sets a descriptive `User-Agent` containing a contact email, read from
  `config.toml`. We hit several hundred careers hosts on a twice-daily schedule; anonymous traffic
  at that shape is what gets an IP blocked.
- Every source client rate-limits per host, from `config.toml`'s `[rate_limits]`. Unknown platforms
  fall back to 1 req/s. Workday is deliberately 0.5 req/s: it throttles fast paging and sits behind
  Akamai bot management.
- Check each new host's `robots.txt` and honour it, both `Crawl-delay` and `Disallow`. Where it is
  stricter than the configured rate, it wins. A host that disallows the path is **not wired**,
  however useful it is and however well it works when probed. Record it in `SOURCES.md` and set the
  registry row to `status = probed_not_used`, so the next session does not re-probe it and reach a
  different answer by accident.
- LinkedIn, Indeed, JobStreet/JobsDB and Glassdoor are out of scope by their terms of service. Do
  not add them, and do not add a proxy or aggregator that resells them.
- Responses are validated against `SourcePosting` at the ingest boundary before writing. A silently
  changed upstream payload must fail loudly, not write a table of nulls that reads as a hiring
  freeze.
- Retries use `tenacity` with exponential backoff, capped at 5 attempts. A 429 backs off for at
  least 60 seconds.

## Absence is not closure

This is the rule that costs the most to get wrong, and it is enforced in `transform/lifecycle.py`
rather than here — but ingest is what makes it possible.

Every ingest run writes a **fetch outcome** per firm alongside the postings: whether the request
succeeded, and how many rows came back. `lifecycle.py` may only mark a posting `closed` if that
firm's fetch in the current snapshot succeeded *and* returned at least one row. Anything else is
`unknown`, carrying forward the previous status.

Without that, one Workday tenant returning a 500 flips eighty live roles to `closed` and fires
eighty Telegram alerts. The finance repo has the same bug in a quieter form: `--skip mas_archive`
went green in CI while halving `regulatory_events` from 1,947 rows to 1,357, because the transform
resolved inputs from a partition where the file simply was not there. A zero-row read and a
genuinely empty board are indistinguishable in the data and must be distinguished in the metadata.
