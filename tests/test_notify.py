"""Telegram digest rendering.

The chunking test is the one that matters. Telegram hard-rejects anything over 4,096
characters, and the first version of `render` only split between family groups — so a group
bigger than the limit could never be split at all. A live digest with forty ByteDance roles
under "SWE & Technical" produced a single 7,416-character block and the send failed.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from gradtrack.notify.telegram import MAX_MESSAGE_CHARS, render, select_new

SNAPSHOT = date(2026, 8, 8)

# Telegram's own hard limit. MAX_MESSAGE_CHARS sits below it as headroom.
TELEGRAM_LIMIT = 4096


def frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(
        rows,
        schema={
            "title": pl.Utf8,
            "firm_name": pl.Utf8,
            "apply_url": pl.Utf8,
            "family_group": pl.Utf8,
            "posted_date": pl.Date,
            "first_seen": pl.Date,
            "is_grad": pl.Boolean,
            "is_internship": pl.Boolean,
            "is_singapore": pl.Boolean,
            "status": pl.Utf8,
        },
    )


def role(n: int, group: str = "SWE & Technical") -> dict:
    return {
        "title": f"Backend Engineer Graduate (Recommendation Architecture {n}) - 2026 Start",
        "firm_name": "ByteDance / TikTok",
        "apply_url": f"https://www.mycareersfuture.gov.sg/job/information-technology/x-{n}",
        "family_group": group,
        "posted_date": SNAPSHOT,
        "first_seen": SNAPSHOT,
        "is_grad": True,
        "is_internship": False,
        "is_singapore": True,
        "status": "open",
    }


class TestChunking:
    def test_a_single_oversized_group_is_split(self) -> None:
        """Forty roles in one family. The old renderer emitted one 7,416-char block."""
        messages = render(frame([role(n) for n in range(40)]), SNAPSHOT)
        assert len(messages) > 1
        for message in messages:
            assert len(message) <= TELEGRAM_LIMIT

    @pytest.mark.parametrize("count", [1, 5, 25, 60, 200])
    def test_no_chunk_ever_exceeds_the_limit(self, count: int) -> None:
        messages = render(frame([role(n) for n in range(count)]), SNAPSHOT)
        assert messages
        for message in messages:
            assert len(message) <= TELEGRAM_LIMIT, f"{len(message)} chars at count={count}"

    def test_every_role_survives_the_split(self) -> None:
        """Chunking must not drop rows — the failure mode nobody notices."""
        messages = render(frame([role(n) for n in range(60)]), SNAPSHOT)
        joined = "\n".join(messages)
        for n in range(60):
            assert f"Architecture {n})" in joined

    def test_a_continuation_chunk_repeats_the_group_header(self) -> None:
        messages = render(frame([role(n) for n in range(40)]), SNAPSHOT)
        assert all("SWE &amp; Technical" in m for m in messages[1:])

    def test_multiple_groups_are_kept_separate(self) -> None:
        rows = [role(n, "SWE & Technical") for n in range(3)]
        rows += [role(n, "Quant & Trading") for n in range(3, 6)]
        joined = "\n".join(render(frame(rows), SNAPSHOT))
        assert "Quant &amp; Trading" in joined
        assert "SWE &amp; Technical" in joined

    def test_headroom_below_the_hard_limit(self) -> None:
        assert MAX_MESSAGE_CHARS < TELEGRAM_LIMIT


class TestEscaping:
    def test_ampersands_in_urls_are_escaped(self) -> None:
        """An unescaped & inside href makes Telegram reject the whole message."""
        rows = [role(0)]
        rows[0]["apply_url"] = "https://example.com/job?a=1&b=2&c=3"
        message = render(frame(rows), SNAPSHOT)[0]
        assert "&amp;b=2" in message
        assert "?a=1&b=2" not in message

    def test_angle_brackets_in_titles_are_escaped(self) -> None:
        rows = [role(0)]
        rows[0]["title"] = "Engineer <script>alert(1)</script>"
        message = render(frame(rows), SNAPSHOT)[0]
        assert "<script>" not in message
        assert "&lt;script&gt;" in message


class TestSelection:
    def test_only_open_singapore_graduate_non_internship_priority_roles(self) -> None:
        rows = [
            role(0),
            {**role(1), "is_grad": False},
            {**role(2), "is_internship": True},
            {**role(3), "is_singapore": False},
            {**role(4), "status": "closed"},
            {**role(5), "family_group": "Legal"},
        ]
        assert select_new(frame(rows), None).height == 1

    def test_since_filters_on_first_seen(self) -> None:
        rows = [role(0), {**role(1), "first_seen": date(2026, 8, 1)}]
        assert select_new(frame(rows), date(2026, 8, 5)).height == 1
