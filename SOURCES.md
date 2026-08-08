# Sources

One entry per **platform**, not per firm — there is one client per ATS and the firms that use it
live in `data/firms/registry.csv`. Never wire a platform in without recording it here.

Fragility: **low** = documented, stable, versioned API · **medium** = official but undocumented or
partly scraped · **high** = undocumented endpoint, bot management, changes without notice.

| Platform | Endpoint | Method | Auth | Rate | Link lands on | Fragility | Status |
|---|---|---|---|---|---|---|---|
| Greenhouse | `boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true` | GET | none | 2/s | **firm's own domain** | low | **wired** |
| Lever | `api.lever.co/v0/postings/{slug}?mode=json` | GET | none | 2/s | `jobs.lever.co` | low | **wired** |
| Ashby | `api.ashbyhq.com/posting-api/job-board/{name}` | GET | none | 2/s | `jobs.ashbyhq.com` | low | **wired** |
| SmartRecruiters | `api.smartrecruiters.com/v1/companies/{id}/postings` | GET | none | 2/s | `jobs.smartrecruiters.com` | low | **wired** |
| Workable | `apply.workable.com/api/v1/widget/accounts/{account}?details=true` | GET | none | 2/s | `apply.workable.com` | medium | **wired** |
| Workday | `{host}/wday/cxs/{tenant}/{site}/jobs` | POST | none | 0.5/s | firm's Workday subdomain | high | **wired** |
| SAP SuccessFactors | per-tenant career site | GET | none | 1/s | firm's own domain | high | planned (Phase 5, browser) |
| Oracle Recruiting Cloud | `{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions` | GET | none | 1/s | firm's Oracle subdomain | high | planned |
| Eightfold | `{host}/api/apply/v2/jobs` | GET | none | 1/s | firm's Eightfold subdomain | high | planned |
| Phenom People | per-tenant widget JSON | GET | none | 1/s | firm's own domain | high | planned |
| MyCareersFuture | `api.mycareersfuture.gov.sg/v2/jobs` | GET | none | 2/s | `mycareersfuture.gov.sg` | medium | **wired** (secondary) |

### On "the company's own site"

Only Greenhouse returns links on the employer's own domain — verified live, Jane Street resolves to
`janestreet.com/join-jane-street/apply/{id}` and Coinbase to `coinbase.com/careers/positions/{id}`.
The other ATS platforms keep the candidate on their own host.

That still satisfies the requirement, and the distinction is worth being precise about: an
ATS-hosted board is the employer's *own official* application system, configured by them and
usually the first place a role appears. It is not an aggregator listing. The reason for reading
these feeds rather than a job board was never the URL — it is that firms publish here days before
they syndicate to MyCareersFuture or LinkedIn.

## Excluded by terms of service

Not probed, not wired, not to be revisited. These forbid automated collection in their terms
regardless of what `robots.txt` says or what technically works.

| Source | Reason |
|---|---|
| LinkedIn Jobs | ToS prohibits scraping and automated access |
| Indeed | ToS prohibits scraping |
| JobStreet / JobsDB (SEEK) | ToS prohibits scraping |
| Glassdoor | ToS prohibits scraping |

## Unavailable

| Source | Reason |
|---|---|
| NUS TalentConnect | login-walled, student SSO |
| NTU CareerAxess | login-walled, student SSO |
| SMU OnTRAC | login-walled, student SSO |

Recorded rather than silently omitted: some firms recruit graduates exclusively through university
portals, and those roles will never appear here. A known gap, not a bug.

---

## Per-platform notes

### Greenhouse

No auth, no paging — the whole board comes back at once. `content=true` is not optional: without
it only the title is available, and for a source with no structured experience field the
description is most of what the classifier has.

Job objects carry `id`, `title`, `location`, `absolute_url`, `updated_at`, `first_published`,
`requisition_id`, `application_deadline`, `company_name`, and sometimes `education`.
`first_published` is the `posted_date` source; `updated_at` is a fallback with basis `updated`.

`company_name` is what makes Greenhouse boards *verifiable* during discovery — see below.

### Workday

The highest-value platform (most banks and large MNCs) and the most fragile. Everything here was
measured against live tenants on 2026-08-08.

- **`limit` above 20 returns an empty `jobPostings` array with no error.** Asking for 100 looks
  exactly like a firm with no openings. The single easiest way to silently lose a bank.
- **Narrow with `searchText`, do not page the world.** Unfiltered totals: Micron 2,718, NVIDIA
  2,000, Salesforce 1,495, DBS 1,353. `searchText: "Singapore"` cuts these to 878, 93 and 266.
- **Multi-location postings hide their locations.** NVIDIA's Singapore results return
  `locationsText: "2 Locations"`, so a substring filter on that field drops them even though the
  search matched. They are kept and resolved from the detail response.
- **Descriptions are one request each**, and 878 for one firm is unaffordable twice a day. Detail
  fetching runs on a budget (80/firm), spent first on graduate-shaped titles; the overflow is kept
  with title-only classification and the count is reported in the fetch outcome.
- **Dates are relative strings** — "Posted Today", "Posted 3 Days Ago", "Posted 30+ Days Ago". The
  last is unresolvable and becomes a null date rather than a fabricated one.
- **The data-centre number (wd1/wd3/wd5/wd12) is not derivable from the tenant**, so the registry
  stores `ats_host` in full.
- **HTTP 422 means the host is right and the site name is wrong**; 404 means the host is wrong.
  Useful when hunting a tenant by hand — `gic.wd3.myworkdayjobs.com` answers 422, so GIC has a
  Workday tenant under some site name we have not found yet.

Verified tenants: `nvidia.wd5/NVIDIAExternalCareerSite`, `salesforce.wd12/External_Career_Site`,
`dbs.wd3/DBS_Careers`, `micron.wd1/External`.

### MyCareersFuture

`robots.txt` is `User-agent: *` / `Disallow:` — everything permitted. A government-operated public
API with no key.

**Secondary source only.** Used for discovery (an employer posting here that is not in the registry
is a signal to add it), cross-check, and salary enrichment. It never supplies `apply_url` when an
ATS row exists for the same role.

Fields that matter: `minimumYearsExperience`, `positionLevels`, `categories`, `salary`,
`postedCompany.uen` (ACRA number — the reliable join key), `hiringCompany` (the real employer when
an agency posts on behalf), `metadata.jobDetailsUrl`, and the date set.

Two traps, both verified against live data:

1. **`expiryDate` is a posting TTL, not an application deadline.** Sampled: 07-30→08-30,
   07-24→08-23, 08-03→09-02. Always about thirty days. Never surfaced as a deadline.
2. **`newPostingDate` is the repost date.** A role created 2026-05-13 and reposted 2026-07-14 shows
   `newPostingDate: 2026-07-14`. `originalPostingDate` is the one that means what a reader assumes.

A third thing, which is a firm-selection problem rather than a data problem: the pool is mostly
staffing agencies and small businesses. A 2,979-posting sample contained 56 postings titled exactly
"MANAGEMENT ASSOCIATE" from MORE YOGURT PREMIUM, TOTAL MANPOWER, FOCUS MANPOWER and DAY ONE. Hence
the registry gate in `transform/dedupe.py`.

---

## Discovery, and how it goes wrong

`uv run python -m gradtrack.discover` guesses ATS tokens from company names and probes each
platform. It is the only practical way to wire several hundred firms, and it is dangerous, because
a wrong token attributes another company's jobs to a firm you are watching — and unlike a coverage
gap, that is invisible.

Four failure modes found in live runs, each now a test in `tests/test_discover.py`:

| Probe | Looks like | Actually |
|---|---|---|
| `greenhouse/mas`, 4 jobs | Monetary Authority of Singapore | **Midwest Applied Solutions** |
| `greenhouse/edb`, 19 jobs, calls itself "EDB" | Singapore Economic Development Board | **EnterpriseDB** (`enterprisedb.com`) |
| `ashby/applied`, 262 jobs | Applied Materials | a different "Applied" |
| `workable/goldman-sachs`, board named "Goldman Sachs" | Goldman Sachs | **an empty board** — Workable returns 200 for any account name and titlecases the token |

So a hit is only auto-applied when the board's own self-description agrees:

- the board's reported company name matches the firm, with the shorter side at least five
  characters (so "EDB" cannot subset-match), or
- a sample apply link is on a non-ATS domain that matches the firm — and a non-ATS domain that
  *disagrees* is a hard rejection, which is what catches EnterpriseDB;
- failing both, for Lever and Ashby which report neither, the token must match the firm name on
  `fuzz.ratio` ≥ 90. Not `token_set_ratio`: that scores a subset as a perfect match, which is how
  "applied", "arthur" and "jump" all reached 100 in the first run.
- tokens under four characters and empty boards are never auto-applied.
