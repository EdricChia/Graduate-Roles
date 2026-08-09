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

## Scheduling — the daily 5pm check

Two ways. They are not exclusive; running both just means the data is fresher.

### GitHub Actions (no machine of your own needed)

Already configured, and free on a public repo:

| Workflow | Runs | What it does |
|---|---|---|
| `refresh-workday.yml` | 06:00 UTC = **14:00 SGT** | The 64 Workday tenants. ~110 minutes, so it starts early enough to be finished before the digest. No notification of its own. |
| `refresh.yml` | 09:07 UTC = **17:07 SGT** | Everything else, then the curated rebuild, health report and **one Telegram digest**. |

Four repository **secrets** are required or the run fails at the config step, by design — an
anonymous crawler hitting several hundred careers hosts is what earns an IP block:

`CONTACT_NAME` · `CONTACT_EMAIL` · `TELEGRAM_BOT_TOKEN` · `TELEGRAM_CHAT_ID`

Two optional repository **variables** control what the scheduled digest sends:

`NOTIFY_ROLE_TYPES` — e.g. `Graduate programme, Graduate role`
`NOTIFY_GROUPS` — e.g. `Quant & Trading, SWE & Technical, Data & Analytics`

These exist because `data/subscriptions.json` is gitignored — a chat id identifies a person
and this repo is public — so a workflow run cannot see what you chose in the bot. Without
them the digest sends the six priority groups.

**GitHub's scheduler is not punctual.** Scheduled runs queue with everyone else's and are
routinely late by tens of minutes, worst of all on the hour; `:07` is deliberate. If 17:00
has to mean 17:00, use the local option.

### Windows Task Scheduler (punctual)

```powershell
$action  = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument '-NoProfile -ExecutionPolicy Bypass -File "C:\Users\edric\Desktop\Project\Graduate Position Project\scripts\daily-refresh.ps1"'
$trigger = New-ScheduledTaskTrigger -Daily -At 5pm
Register-ScheduledTask -TaskName "gradtrack-daily" -Action $action -Trigger $trigger
```

`scripts/daily-refresh.ps1` runs the whole pipeline and logs to `reports/runs/`. The Workday
leg makes it about two hours end to end, so for a digest that actually lands at 5pm, schedule
`-WorkdayOnly` a couple of hours earlier and `-SkipWorkday` at 17:00.

### What "new" means

The digest sends postings it has not sent before, tracked by `job_key` rather than by date.
That matters more than it sounds: a date comparison silently skips anything discovered by a
second run on a date already stamped, which is exactly what the Workday leg landing after the
fast one produces.

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
