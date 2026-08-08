"""robots.txt parsing and enforcement.

`.claude/rules/ingest.md` has required this since Phase 0 and it was satisfied by checking
each host by hand as it was added. That does not survive a registry of 183 firms, so it is
enforced in code — and code that enforces a rule needs tests, or the rule quietly stops being
true again.
"""

from __future__ import annotations

import httpx
import pytest

from gradtrack.sources.robots import (
    RobotsCache,
    RobotsVerdict,
    disallowed_paths,
    workday_site_candidates,
)

TEMASEK_ROBOTS = """
User-agent: *
Disallow: /applybutton/
Disallow: /talentcommunity/
Disallow: /mobile/talentcommunity/
Disallow: /emailsubscribe/
Disallow: /services/
Disallow: /preapply/
Disallow: /error
Disallow: /unsubscribe/
Disallow: /reset/
"""

DBS_ROBOTS = """
User-agent: *
Disallow: /DBS_Careers/
Disallow: /refreshFacet/
"""

NVIDIA_ROBOTS = """
User-agent: *
Disallow: /talentcommunity/
"""


def cache_for(bodies: dict[str, tuple[int, str]]) -> RobotsCache:
    """A RobotsCache backed by canned responses rather than the network."""

    def handler(request: httpx.Request) -> httpx.Response:
        status, body = bodies.get(request.url.host, (404, ""))
        return httpx.Response(status, text=body, headers={"content-type": "text/plain"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return RobotsCache(client=client, user_agent="gradtrack")


class TestEnforcement:
    def test_a_disallowed_path_is_refused(self) -> None:
        cache = cache_for({"jobs.temasek.com.sg": (200, TEMASEK_ROBOTS)})
        assert (
            cache.verdict("https://jobs.temasek.com.sg/talentcommunity/x")
            is RobotsVerdict.DISALLOWED
        )

    def test_a_permitted_path_is_allowed(self) -> None:
        cache = cache_for({"jobs.temasek.com.sg": (200, TEMASEK_ROBOTS)})
        assert (
            cache.verdict("https://jobs.temasek.com.sg/tile-search-results/?q=")
            is RobotsVerdict.ALLOWED
        )

    def test_a_prefix_that_does_not_match_is_allowed(self) -> None:
        """DBS disallows /DBS_Careers/ — the CXS API lives under /wday/cxs/ and is not that.

        This is the judgement the standard actually specifies, and doing it with the standard
        library's matcher rather than by eye is the whole point of the module.
        """
        cache = cache_for({"dbs.wd3.myworkdayjobs.com": (200, DBS_ROBOTS)})
        allowed = "https://dbs.wd3.myworkdayjobs.com/wday/cxs/dbs/DBS_Careers/jobs"
        refused = "https://dbs.wd3.myworkdayjobs.com/DBS_Careers/job/x"
        assert cache.verdict(allowed) is RobotsVerdict.ALLOWED
        assert cache.verdict(refused) is RobotsVerdict.DISALLOWED

    def test_a_missing_file_means_unrestricted(self) -> None:
        cache = cache_for({})
        assert cache.verdict("https://example.com/jobs") is RobotsVerdict.ALLOWED

    @pytest.mark.parametrize("status", [401, 403])
    def test_a_withheld_file_means_full_disallow(self, status: int) -> None:
        cache = cache_for({"example.com": (status, "")})
        assert cache.verdict("https://example.com/jobs") is RobotsVerdict.DISALLOWED

    def test_an_unreachable_file_is_unknown_not_permission(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        cache = RobotsCache(client=httpx.Client(transport=httpx.MockTransport(handler)))
        assert cache.verdict("https://example.com/jobs") is RobotsVerdict.UNKNOWN
        assert cache.can_fetch("https://example.com/jobs") is True
        assert cache.can_fetch("https://example.com/jobs", unknown_is_allowed=False) is False

    def test_one_fetch_per_host(self) -> None:
        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, text=TEMASEK_ROBOTS, headers={"content-type": "text/plain"})

        cache = RobotsCache(client=httpx.Client(transport=httpx.MockTransport(handler)))
        for _ in range(5):
            cache.verdict("https://jobs.temasek.com.sg/a")
        assert len(calls) == 1


class TestWorkdaySiteCandidates:
    def test_the_site_id_is_read_out_of_disallow(self) -> None:
        """DBS names its career site in robots.txt, which is what makes discovery cheap."""
        assert workday_site_candidates(DBS_ROBOTS) == ["DBS_Careers"]

    def test_operational_paths_are_not_site_ids(self) -> None:
        assert workday_site_candidates(NVIDIA_ROBOTS) == []

    def test_a_tenant_that_names_nothing_yields_nothing(self) -> None:
        assert workday_site_candidates("User-agent: *\nDisallow:\n") == []

    def test_blanket_disallow_is_not_a_site(self) -> None:
        assert workday_site_candidates("User-agent: *\nDisallow: /\n") == []


class TestDisallowedPaths:
    def test_paths_are_listed_in_order(self) -> None:
        paths = disallowed_paths(TEMASEK_ROBOTS)
        assert paths[0] == "/applybutton/"
        assert "/talentcommunity/" in paths
