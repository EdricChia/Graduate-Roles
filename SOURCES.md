# Sources

One entry per **platform**, not per firm — there is one client per ATS and the firms that use it
live in `data/firms/registry.csv`. Never wire a platform in without recording it here.

Fragility: **low** = documented, stable, versioned API · **medium** = official but undocumented or
partly scraped · **high** = undocumented endpoint, bot management, changes without notice.

| Platform | Endpoint | Method | Auth | Rate | Fragility | Status |
|---|---|---|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | GET | none | 2 req/s | low | **wired** (Phase 3) |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | GET | none | 2 req/s | low | planned (Phase 3) |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{name}` | GET | none | 2 req/s | low | planned (Phase 3) |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{id}/postings` | GET | none | 2 req/s | low | planned (Phase 3) |
| Workable | `apply.workable.com/api/v1/widget/accounts/{account}` | GET | none | 2 req/s | medium | planned (Phase 3) |
| Workday | `{host}/wday/cxs/{tenant}/{site}/jobs` | POST | none | 0.5 req/s | high | planned (Phase 4) |
| SAP SuccessFactors | per-tenant career site, `rmkcdn.successfactors.com` assets | GET | none | 1 req/s | high | planned (Phase 4) |
| Oracle Recruiting Cloud | `{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions` | GET | none | 1 req/s | high | planned (Phase 4) |
| Eightfold | `{host}/api/apply/v2/jobs` | GET | none | 1 req/s | high | planned (Phase 4) |
| Phenom People | per-tenant widget JSON | GET | none | 1 req/s | high | planned (Phase 4) |
| Playwright fallback | rendered careers page, per firm | — | none | 1 req/s | high | planned (Phase 5) |
| MyCareersFuture | `api.mycareersfuture.gov.sg/v2/jobs` | GET/POST | none | 2 req/s | medium | planned (Phase 2) |

## Excluded by terms of service

Not probed, not wired, and not to be revisited. These forbid automated collection in their terms
regardless of what `robots.txt` says or what technically works.

| Source | Reason |
|---|---|
| LinkedIn Jobs | ToS prohibits scraping and automated access |
| Indeed | ToS prohibits scraping |
| JobStreet / JobsDB (SEEK) | ToS prohibits scraping |
| Glassdoor | ToS prohibits scraping |

The cost of this exclusion is smaller here than it would be for a generic job tracker, because the
whole premise is reading company career sites *before* they syndicate to boards.

## Unavailable

| Source | Reason |
|---|---|
| NUS TalentConnect | login-walled, student SSO |
| NTU CareerAxess | login-walled, student SSO |
| SMU OnTRAC | login-walled, student SSO |

Recorded rather than silently omitted: some firms recruit graduates exclusively through university
portals, and those roles will never appear in this tracker. That is a known coverage gap, not a
bug.

## Per-platform notes

### Greenhouse

No auth. `?content=true` returns the full HTML description, which the classifier needs — without
it only the title is available and grad detection loses its strongest description signals.

The important property for this project: `absolute_url` points at the **firm's own domain**, not at
Greenhouse. Verified live — Jane Street returns `janestreet.com/join-jane-street/apply/{id}` and
Coinbase returns `coinbase.com/careers/positions/{id}`. So the clean-JSON layer already satisfies
the "link to the company career site" requirement without any extra work.

Job objects carry `id`, `title`, `location`, `absolute_url`, `updated_at`, `first_published`,
`requisition_id`, `application_deadline`, and sometimes `education`. `first_published` is the
`posted_date` source; `updated_at` is a fallback with basis `updated`.

### Workday

The highest-value platform — most banks and large MNCs run on it — and the most fragile.

- POST, not GET. Body: `{"limit": 20, "offset": 0, "searchText": "", "appliedFacets": {}}`.
- **`limit` above 20 returns an empty `jobPostings` array with no error.** Asking for 100 looks
  exactly like a firm with no openings. This is the single easiest way to silently lose a bank.
- The list view returns relative date strings ("Posted 3 Days Ago"), not timestamps, so
  `posted_date` needs the detail request per posting or a relative-date parse.
- Akamai bot management blocks naive single-IP paging. Hence 0.5 req/s and full backoff.
- The data-centre number in the host (`wd1`/`wd3`/`wd5`) is not derivable from the tenant name, so
  the registry stores `ats_host` in full rather than reconstructing it.

### MyCareersFuture

`robots.txt` is `User-agent: *` / `Disallow:` (empty) — everything permitted, plus a sitemap. A
government-operated public API with no key.

Used as a **secondary** source only: for discovery (a firm posting here that is not in the registry
is a signal to add it), for cross-check, and for salary enrichment. It never supplies `apply_url`
when an ATS row exists for the same role, because firms post here days after their own site.

Fields that matter: `minimumYearsExperience`, `positionLevels`, `categories`, `salary`,
`postedCompany.uen` (ACRA registry number, the reliable join key to a firm), and the date set
`createdAt` / `newPostingDate` / `originalPostingDate` / `expiryDate`.

Two traps, both verified against live data:

1. **`expiryDate` is a posting TTL, not an application deadline.** Sampled: `07-30 → 08-30`,
   `07-24 → 08-23`, `08-03 → 09-02`. Always about 30 days. It must never be surfaced as a deadline.
2. **`newPostingDate` is the repost date.** A role created 2026-05-13 and reposted 2026-07-14 shows
   `newPostingDate: 2026-07-14`. Use `originalPostingDate` for `posted_date`, or the tracker will
   report months-old listings as fresh.
