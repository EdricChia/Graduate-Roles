# Graduate-Roles

Tracks graduate-level openings in Singapore at large, well-paying, prestigious employers — banks,
quant firms, big tech, crypto, consulting, sovereign entities like GIC and Temasek, and the MNCs.

It reads **company career sites directly**, not job boards. Firms publish to their own applicant
tracking system first and syndicate to MyCareersFuture and LinkedIn days later, so a tracker built
on aggregators is always late. Every row links to the firm's own careers page.

## What it tracks

For each opening: the firm, the role, its job family, when it was posted, whether it is still
open, and the link. Openings are grouped into families — Strategy Consulting, Management
Consulting, Quant, Software Engineering, Data Science, Data Analyst, Business Analyst, Operations,
Supply Chain, Strategy & Operations, Investment, and others — and the six priority groups are
pushed to Telegram when something new appears.

Scope is graduate programmes, graduate-level openings, and entry-level roles that state fresh
graduate or zero years of experience. Internships are classified and stored but hidden by default.

Current coverage: **55 wired firms** across six ATS platforms, **2,419 Singapore postings**,
**100 graduate-level roles**. A further 128 firms are in the registry awaiting an identified
ATS — see `BUILD_SPEC.md`.

## Quick start

```bash
uv sync
cp config.toml.example config.toml     # fill in a real contact email
uv run pytest
uv run streamlit run app.py
```

## How it works

```
data/firms/registry.csv          one row per firm: which ATS, which token
        │
        ▼
sources/<platform>.py            one client per ATS, never per firm
        │
        ▼
data/raw/source=<platform>/snapshot_date=<date>/     append-only, never rewritten
        │
        ▼
transform/  normalise → classify → lifecycle → dedupe
        │
        ▼
data/curated/postings.parquet    →  app.py  ·  notify/telegram.py
```

Adding a company is a row in `registry.csv`, not a code change. That is what makes several hundred
firms tractable.

## Documentation

| File | What it covers |
|---|---|
| `CLAUDE.md` | Layout, commands, design principles |
| `BUILD_SPEC.md` | The phased build plan and each phase's acceptance criteria |
| `SOURCES.md` | Every platform: endpoint, limits, fragility — and everything excluded, with reasons |
| `TAXONOMY.md` | Job family definitions and the classification rules |
| `DATA_CONTRACT.md` | The curated schema (written at the end of Phase 2) |
