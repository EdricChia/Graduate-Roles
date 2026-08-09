"""Find a firm's Workday tenant: which host, and which career site on it.

    uv run python -m gradtrack.discover_workday --firm gic
    uv run python -m gradtrack.discover_workday --apply

Workday is where most banks and large MNCs are, and it is the reason 154 firms sit at `todo`.
Unlike Greenhouse, a Workday board needs three values and only one of them is guessable: the
host carries a data-centre number (`wd1`, `wd3`, `wd5`, `wd12`, `wd103`) that is assigned, not
derived, and the site id is whatever the tenant's admin called it.

Brute-forcing host × site is thousands of requests per firm. Two observations make it cheap
instead.

**`robots.txt` identifies the host.** `https://{tenant}.wd{n}.myworkdayjobs.com/robots.txt`
returns 200 when the tenant exists there and 422 when it does not. Eight data-centre numbers
is eight cheap GETs, and the answer is unambiguous.

This also corrects an inference recorded earlier in `SOURCES.md`: a 422 from the jobs endpoint
was read as "host right, site wrong". It is not. `gic.wd3` returns 422 for `robots.txt` as
well, and a tenant that exists serves that file — so 422 means the tenant is not on that host
at all.

**`robots.txt` often names the site.** DBS returns `Disallow: /DBS_Careers/`, and
`DBS_Careers` is exactly the site id the CXS endpoint wants. Not every tenant lists one —
Salesforce and Micron disallow nothing — so a fallback list of conventional names covers the
rest.

Verification is stronger here than for Greenhouse and Lever. A `myworkdayjobs.com` subdomain
is provisioned for the tenant, so `dbs.wd3.myworkdayjobs.com` belonging to anyone but DBS is
not a realistic failure mode. The checks that remain are that the tenant token resembles the
firm and that the board actually returns postings.
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass

import httpx
from rapidfuzz import fuzz

from gradtrack.config import Config, load_config
from gradtrack.firms import Firm, FirmStatus, load_registry
from gradtrack.schema import Platform
from gradtrack.sources.base import RateLimiter, build_client, contains_singapore, post_json
from gradtrack.sources.robots import workday_site_candidates

# Observed in the wild. Ordered by how common they are, so the usual answer comes first.
# Six rather than every number Workday has ever issued: the search space is multiplicative
# across firms and candidates, and the long tail is rare enough that a hand-added registry
# row is cheaper than probing for it everywhere.
DATA_CENTRES = ("wd1", "wd3", "wd5", "wd2", "wd12", "wd103")

# The robots.txt probe gets its own rate, separate from the 0.5 req/s the jobs API needs.
# That figure exists because the CXS endpoint throttles paging and sits behind Akamai; a
# static file does not. At 0.5 req/s a full sweep would take over four hours, which in
# practice means it never gets run — and a politeness setting so strict that the tool goes
# unused protects nobody.
DISCOVERY_RATE = 3.0
# Two candidates per firm, not five. Same multiplicative reasoning as the data centres.
MAX_TENANT_CANDIDATES = 3
# Every candidate site on a host is probed so the best can be chosen; this bounds it.
MAX_SITES_PER_HOST = 8

ROBOTS_URL = "https://{host}/robots.txt"
JOBS_URL = "https://{host}/wday/cxs/{tenant}/{site}/jobs"

# Tried after anything robots.txt named. `{t}` is the tenant, `{T}` its capitalised form.
SITE_TEMPLATES = (
    "External",
    "{T}_Careers",
    "{T}Careers",
    "External_Career_Site",
    "ExternalCareerSite",
    "{T}_External_Career_Site",
    "Careers",
    "{T}_External",
    "{T}_Careers_External",
    "Global_Careers",
    "GlobalCareers",
    "{T}careers",
    "External_Careers",
    "careers",
)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_NOISE = re.compile(
    r"\b(inc|ltd|limited|llc|llp|plc|pte|group|holdings|international|company|co|corp|"
    r"corporation|the|and|of|technologies|technology|global|asia|pacific|singapore|bank)\b"
)
TENANT_MATCH_THRESHOLD = 78

# A candidate has to be a legal DNS label before it can be a hostname. Without this, a firm
# whose name denoises to nothing yields an empty token, the probe builds
# ".wd1.myworkdayjobs.com", and Python's IDNA encoder raises UnicodeError — which is not an
# httpx error, escaped the handler below, and killed an entire 25-minute sweep before any of
# its four confirmed tenants were written to the registry.
_VALID_TENANT = re.compile(r"^[a-z0-9][a-z0-9-]{0,28}[a-z0-9]$")


def tenant_candidates(firm: Firm) -> list[str]:
    """Plausible Workday tenant names, most likely first."""
    name = firm.firm_name.lower()
    stripped = _NOISE.sub(" ", name).strip()
    ordered = [
        firm.firm_id,
        _NON_ALNUM.sub("", stripped),
        _NON_ALNUM.sub("", name),
        stripped.split()[0] if stripped.split() else "",
        # Initialism: "Standard Chartered" -> "sc", "Goldman Sachs" -> "gs". Workday tenants
        # are frequently the ticker or the internal abbreviation.
        "".join(word[0] for word in stripped.split() if word)[:6],
    ]
    out: list[str] = []
    for token in ordered:
        if _VALID_TENANT.match(token) and token not in out:
            out.append(token)
    return out


@dataclass(frozen=True)
class WorkdayHit:
    firm_id: str
    firm_name: str
    host: str
    tenant: str
    site: str
    total: int
    singapore: int
    site_from_robots: bool

    def verify(self) -> tuple[bool, str]:
        if self.total <= 0:
            return False, "board returns no postings"
        score = fuzz.partial_ratio(
            _NON_ALNUM.sub("", self.tenant),
            _NON_ALNUM.sub("", _NOISE.sub(" ", self.firm_name.lower())),
        )
        if score < TENANT_MATCH_THRESHOLD:
            return (
                False,
                f"tenant {self.tenant!r} does not resemble {self.firm_name!r} ({score:.0f})",
            )
        source = "robots.txt" if self.site_from_robots else "conventional name"
        return True, f"{self.total} postings ({self.singapore} SG), site from {source}"

    @property
    def verified(self) -> bool:
        return self.verify()[0]


def _site_rank(hit: WorkdayHit) -> tuple[int, int, int]:
    """How good a site is for this firm, best last.

    Singapore roles first, because that is what the tracker is for and it is the signal that
    separated TrafiguraCareerSite (4) from Puma_Energy_Careers (1). Then whether the site
    name looks like the firm, which breaks ties on tenants where every site is currently
    empty — Unilever's are, and the name is the only thing distinguishing its graduate board
    from an internal one. Total postings last.
    """
    resembles = fuzz.partial_ratio(
        _NON_ALNUM.sub("", hit.site.lower()),
        _NON_ALNUM.sub("", _NOISE.sub(" ", hit.firm_name.lower())),
    )
    return hit.singapore, int(resembles), hit.total


def find_hosts(
    client: httpx.Client, limiter: RateLimiter, tenant: str
) -> list[tuple[str, list[str]]]:
    """Hosts where this tenant exists, with any site ids their robots.txt names.

    Rate-limited like everything else. A miss still reaches Workday — every
    `*.myworkdayjobs.com` name resolves and the server answers 422 — so 154 firms times five
    candidate tenants times ten data centres is several thousand real requests to one
    operator. Sending them as fast as the network allows is exactly what
    `.claude/rules/ingest.md` forbids, and a discovery tool is not exempt from it.
    """
    found: list[tuple[str, list[str]]] = []
    for centre in DATA_CENTRES:
        host = f"{tenant}.{centre}.myworkdayjobs.com"
        limiter.wait()
        try:
            response = client.get(ROBOTS_URL.format(host=host), timeout=20)
        except Exception:  # noqa: BLE001
            # Deliberately broad. A probe is expected to fail most of the time, and the
            # failures are not all httpx's: a malformed hostname raises UnicodeError from the
            # IDNA codec, deep in socket resolution. One bad candidate must cost one request,
            # not the whole sweep.
            continue
        # A tenant that exists serves robots.txt. 422 is Workday's "no such tenant here".
        if response.status_code != 200:
            continue
        found.append((host, workday_site_candidates(response.text)))
    return found


def probe_site(
    client: httpx.Client, limiter: RateLimiter, host: str, tenant: str, site: str
) -> tuple[int, int] | None:
    """(total, singapore) if this site id serves postings, else None."""
    try:
        payload = post_json(
            client,
            JOBS_URL.format(host=host, tenant=tenant, site=site),
            limiter,
            json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "Singapore"},
        )
    except Exception:  # noqa: BLE001 - a wrong site id is the expected outcome here
        return None
    if not isinstance(payload, dict):
        return None
    postings = payload.get("jobPostings") or []
    if not postings:
        return None
    singapore = sum(1 for p in postings if contains_singapore(p.get("locationsText", "")))
    return int(payload.get("total", len(postings))), singapore


def discover_firm(
    client: httpx.Client,
    config: Config,
    firm: Firm,
    probe_limiter: RateLimiter | None = None,
) -> list[WorkdayHit]:
    limiter = RateLimiter(config.rate_limit_for(Platform.WORKDAY.value))
    probe_limiter = probe_limiter or RateLimiter(DISCOVERY_RATE)
    hits: list[WorkdayHit] = []
    for tenant in tenant_candidates(firm)[:MAX_TENANT_CANDIDATES]:
        for host, robots_sites in find_hosts(client, probe_limiter, tenant):
            candidates = list(robots_sites)
            capitalised = tenant.capitalize()
            for template in SITE_TEMPLATES:
                site = template.format(t=tenant, T=capitalised)
                if site not in candidates:
                    candidates.append(site)
            # Probe every candidate and pick the best, rather than stopping at the first that
            # answers. A tenant routinely hosts several sites and the first is often the
            # wrong one: Trafigura lists Puma_Energy_Careers ahead of TrafiguraCareerSite,
            # so first-wins attributed an affiliate's postings to Trafigura and found one
            # Singapore role where the right site has four. Golden Agri listed SMART_Careers
            # ahead of GAR_InternationalCareers, one Singapore role against five. Unilever
            # offers TMICC ahead of Unilever_Early_Careers, which is its graduate board.
            found: list[WorkdayHit] = []
            for site in candidates[:MAX_SITES_PER_HOST]:
                result = probe_site(client, limiter, host, tenant, site)
                if result is None:
                    continue
                total, singapore = result
                found.append(
                    WorkdayHit(
                        firm.firm_id,
                        firm.firm_name,
                        host,
                        tenant,
                        site,
                        total,
                        singapore,
                        site in robots_sites,
                    )
                )
            if found:
                hits.append(max(found, key=_site_rank))
                break
        if hits:
            break
    return hits


def apply_hits(config: Config, hits: list[WorkdayHit], *, force: bool = False) -> list[str]:
    path = config.registry_path
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    best = {hit.firm_id: hit for hit in hits if hit.verified}
    applied: list[str] = []
    for row in rows:
        hit = best.get((row.get("firm_id") or "").strip())
        if hit is None:
            continue
        status = (row.get("status") or "").strip()
        # An already-wired row is only corrected under --force, and only when the site
        # actually changed. Re-running discovery must not churn rows it agrees with.
        already_wired = status != FirmStatus.TODO.value
        if already_wired and (not force or row.get("board_site") == hit.site):
            continue
        row.update(
            ats_platform=Platform.WORKDAY.value,
            ats_token=hit.tenant,
            ats_host=hit.host,
            board_site=hit.site,
            robots_status="allowed",
            status=FirmStatus.WIRED.value,
            notes=f"workday tenant discovered; {hit.verify()[1]}",
        )
        applied.append(f"{hit.firm_id} -> {hit.host}/{hit.site}")

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Find Workday tenants for todo firms")
    parser.add_argument("--firm", default="")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--force",
        action="store_true",
        help="also re-probe firms already wired, and correct the site if a better one exists",
    )
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args(argv)

    config = load_config()
    registry = load_registry(config.registry_path)
    if args.firm:
        targets = [registry.by_id(args.firm)]
    elif args.force:
        targets = [
            f
            for f in registry
            if f.status is FirmStatus.TODO
            or (f.ats_platform is not None and f.ats_platform.value == Platform.WORKDAY.value)
        ]
    else:
        targets = [f for f in registry if f.status is FirmStatus.TODO]
    if args.limit:
        targets = targets[: args.limit]

    print(f"probing {len(targets)} firm(s) for Workday tenants\n")
    all_hits: list[WorkdayHit] = []
    probe_limiter = RateLimiter(DISCOVERY_RATE)
    with build_client(config, timeout=25) as client:
        for firm in targets:
            # A sweep is long and its results are only written at the end, so one firm
            # raising must not discard every tenant found before it.
            try:
                found = discover_firm(client, config, firm, probe_limiter)
            except Exception as exc:  # noqa: BLE001
                print(f"  ERR {firm.firm_id:<20} {type(exc).__name__}: {exc}"[:120])
                continue
            for hit in found:
                all_hits.append(hit)
                ok, reason = hit.verify()
                print(
                    f"  {'OK ' if ok else 'no '}{hit.firm_id:<20} {hit.host:<42} "
                    f"{hit.site:<28} {reason}"
                )

    verified = [h for h in all_hits if h.verified]
    print(f"\n{len(all_hits)} tenant(s) found, {len(verified)} verified")
    if args.apply:
        for line in apply_hits(config, all_hits, force=args.force):
            print(f"  wired {line}")
    elif verified:
        print("re-run with --apply to write these into data/firms/registry.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
