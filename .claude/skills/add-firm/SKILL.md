---
name: add-firm
description: Add a company to the tracker — identify its ATS, verify the board really belongs to it, and write the registry row. Use when the user names a firm to track, or when the discovery-candidates report shows an employer worth adding.
---

# Adding a firm

Adding a company is a row in `data/firms/registry.csv`. There is one client per ATS platform and
never one per firm, so this should not require writing code. If it seems to, check whether the
firm is on a platform that already has a client.

## 1. Try the probe first

```bash
uv run python -m gradtrack.discover --firm <firm_id> --show-rejected
```

It guesses tokens from the company name and probes Greenhouse, Lever, Ashby, SmartRecruiters and
Workable. If it reports a verified hit, `--apply` writes the row.

## 2. If the probe finds nothing, identify the ATS by hand

Fetch the firm's careers page and look for a platform signal in the HTML or the network calls:

| Signal in the page | Platform |
|---|---|
| `boards.greenhouse.io`, `job-boards.greenhouse.io`, `gh_jid=` | greenhouse |
| `jobs.lever.co` | lever |
| `jobs.ashbyhq.com`, `api.ashbyhq.com` | ashby |
| `jobs.smartrecruiters.com` | smartrecruiters |
| `apply.workable.com` | workable |
| `*.myworkdayjobs.com` | workday |
| `rmkcdn.successfactors.com` | successfactors |
| `*.oraclecloud.com/hcmUI` | oracle_orc |
| `*.eightfold.ai` | eightfold |
| `phenompeople`, `*.phenom.com` | phenom |

**Workday needs three values, not one.** `ats_host` is the full host including the data-centre
number, which is not derivable from the tenant. `ats_token` is the tenant. `board_site` is the site
name. Probe it directly:

```bash
curl -s -X POST "https://{host}/wday/cxs/{tenant}/{site}/jobs" \
  -H 'Content-Type: application/json' \
  -d '{"appliedFacets":{},"limit":20,"offset":0,"searchText":"Singapore"}'
```

HTTP 422 means the host is right and the site name is wrong — keep trying site names
(`External`, `Careers`, `{Tenant}_Careers`, `External_Career_Site`). HTTP 404 means the host is
wrong; try other `wd` numbers.

## 3. Check robots.txt before wiring anything

```bash
curl -s https://<careers-host>/robots.txt
```

If the path is disallowed, **do not wire it**. Set `status = probed_not_used`, record it in
`SOURCES.md`, and move on. A disallowed board stays in the registry so the next session does not
re-probe it and reach a different answer.

## 4. Verify the board is actually the firm's

This is the step that matters. A board that exists under a plausible token is not evidence that it
belongs to the company you want. Live probes have produced:

- `greenhouse/mas` — Midwest Applied Solutions, not the Monetary Authority of Singapore
- `greenhouse/edb` — EnterpriseDB, calling itself "EDB", with apply links on `enterprisedb.com`
- `ashby/applied` — 262 jobs, not Applied Materials

Confirm at least one of: the board reports the firm's own company name, or an apply link lands on
the firm's own domain, or the token is an exact match for the brand. Open one posting and read it.

A wrong row is worse than a missing one. A gap is visible in the coverage report; a
misattribution silently fills the dashboard with another company's jobs.

## 5. Write the row and confirm it returns something

```
firm_id,firm_name,sector,tier,ats_platform,ats_token,ats_host,board_site,careers_url,robots_status,status,notes
```

`tier` is 1 for the firms the user actively wants and 2 for the wider net. `notes` should record
how the board was verified.

```bash
uv run pytest tests/test_firms.py          # the row validates
uv run python -m gradtrack.ingest.ats --firm <firm_id>
```

A firm with zero Singapore postings is a normal result, not a failure — but check it against the
board in a browser before believing it, because zero is also what a wrong token returns.

Finally, add a line to `SOURCES.md` if this is the first firm on a platform.
