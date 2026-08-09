"""Daily Telegram push for new graduate openings.

    uv run python -m gradtrack.notify.telegram --dry-run
    uv run python -m gradtrack.notify.telegram

Sends roles first seen since the last run, in the six priority family groups, with links to
each firm's own careers page.

Three things it deliberately will not do.

It will not announce a role twice. The last notified snapshot date is kept in
``data/manual/notify_state.json`` and only postings whose ``first_seen`` is after it are sent.

It will not announce closures. A closed role is not actionable, and given the lifecycle
guard, a status change to ``unknown`` means an outage rather than news.

It will not send anything for a firm whose fetch failed. That is the same guard as
`lifecycle.py`, applied one layer up: an outage must never look like activity.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
import polars as pl

from gradtrack.config import Config, load_config
from gradtrack.notify.subscriptions import STORE_FILE, Subscription, SubscriptionStore
from gradtrack.schema import PRIORITY_GROUPS

API_URL = "https://api.telegram.org/bot{token}/sendMessage"
# Telegram rejects anything over 4096 characters, so long digests are split.
MAX_MESSAGE_CHARS = 3800
STATE_FILE = "notify_state.json"


@dataclass(frozen=True)
class NotifyState:
    last_notified: date | None

    @classmethod
    def load(cls, path: Path) -> NotifyState:
        if not path.exists():
            return cls(None)
        try:
            raw = json.loads(path.read_text(encoding="utf-8")).get("last_notified")
            return cls(date.fromisoformat(raw) if raw else None)
        except (ValueError, OSError):
            return cls(None)

    def save(self, path: Path, value: date) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_notified": value.isoformat()}), encoding="utf-8")


def _escape(text: str) -> str:
    """Minimal HTML escaping for Telegram's HTML parse mode."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _base(frame: pl.DataFrame) -> pl.DataFrame:
    """Everything the tracker would ever notify anyone about."""
    return frame.filter(
        pl.col("is_grad")
        & ~pl.col("is_internship")
        & pl.col("is_singapore")
        & (pl.col("status") == "open")
    )


def select_new(frame: pl.DataFrame, since: date | None) -> pl.DataFrame:
    """The default digest: priority family groups, newly seen.

    Used when there are no subscribers — the scheduled workflow's fallback to the single chat
    configured in secrets.
    """
    view = _base(frame).filter(pl.col("family_group").is_in(list(PRIORITY_GROUPS)))
    if since is not None:
        view = view.filter(pl.col("first_seen").is_not_null() & (pl.col("first_seen") > since))
    return view.sort(["family_group", "posted_date"], descending=[False, True], nulls_last=True)


def select_for(frame: pl.DataFrame, sub: Subscription, since: date | None) -> pl.DataFrame:
    """One subscriber's roles: their role types, their family groups, new since `since`.

    ``since=None`` means everything currently open, which is both what `/roles` asks for and
    what a brand-new subscriber gets on their first delivery — the same code path, because a
    new subscriber's ``last_notified`` is null.
    """
    view = _base(frame).filter(
        pl.col("role_type").is_in(sorted(sub.role_types))
        & pl.col("family_group").is_in(sorted(sub.groups))
    )
    if since is not None:
        view = view.filter(pl.col("first_seen").is_not_null() & (pl.col("first_seen") > since))
    return view.sort(["family_group", "posted_date"], descending=[False, True], nulls_last=True)


def render(view: pl.DataFrame, snapshot: date) -> list[str]:
    """Render the digest, split into Telegram-sized chunks.

    Grouped by family so the message is scannable on a phone, and split **line by line**
    rather than group by group. The first version accumulated a whole family group before
    checking the length, which meant a group larger than the limit could never be split: a
    live digest with forty ByteDance roles under "SWE & Technical" produced a single 7,416
    character block against Telegram's 4,096 ceiling, and the send failed outright.

    A group header is repeated at the top of a continuation chunk so a run of roles is never
    left without the family it belongs to.
    """
    if view.is_empty():
        return []

    header = f"<b>🎓 {view.height} new graduate role(s)</b> · {snapshot}"
    chunks: list[str] = []
    current: list[str] = [header]
    length = len(header)
    group_header = ""

    def flush() -> None:
        nonlocal current, length
        chunks.append("\n".join(current))
        current = [f"{header} (cont. {len(chunks) + 1})"]
        length = len(current[0])
        if group_header:
            current.append(group_header)
            length += len(group_header) + 1

    for group in view["family_group"].unique(maintain_order=True).to_list():
        rows = view.filter(pl.col("family_group") == group)
        group_header = f"\n<b>{_escape(group)}</b> ({rows.height})"
        if length + len(group_header) > MAX_MESSAGE_CHARS:
            flush()
        current.append(group_header)
        length += len(group_header) + 1

        for row in rows.iter_rows(named=True):
            posted = row["posted_date"] or row["first_seen"]
            when = posted.isoformat() if posted else "date unknown"
            line = (
                f'• <a href="{_escape(row["apply_url"])}">{_escape(row["title"][:90])}</a>\n'
                f"  {_escape(row['firm_name'][:40])} · {when}"
            )
            if length + len(line) > MAX_MESSAGE_CHARS:
                flush()
            current.append(line)
            length += len(line) + 1

    chunks.append("\n".join(current))
    return chunks


def send(config: Config, messages: list[str], *, chat_id: str = "") -> int:
    """Post each chunk. Returns how many were delivered."""
    chat_id = chat_id or config.telegram_chat_id
    if not config.telegram_bot_token or not chat_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are unset. Create a bot with @BotFather "
            "and add them as repository secrets, or run with --dry-run."
        )
    sent = 0
    with httpx.Client(timeout=30) as client:
        for message in messages:
            response = client.post(
                API_URL.format(token=config.telegram_bot_token),
                json={
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if response.status_code != 200:
                # Telegram puts the real reason in the body and it is always specific —
                # "chat not found", "message is too long", "can't parse entities". Letting
                # httpx raise a bare 400 throws that away and turns a one-line fix into a
                # debugging session.
                detail = ""
                try:
                    detail = str(response.json().get("description", ""))
                except ValueError:
                    detail = response.text[:200]
                hint = ""
                if "chat not found" in detail.lower():
                    hint = (
                        " — open the bot in Telegram and send /start; a bot cannot message a "
                        "user who has never contacted it."
                    )
                raise RuntimeError(f"Telegram rejected the message: {detail}{hint}")
            sent += 1
    return sent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push new graduate openings to Telegram")
    parser.add_argument(
        "--dry-run", action="store_true", help="write reports/digest/<date>.md instead of sending"
    )
    parser.add_argument(
        "--since", default="", help="YYYY-MM-DD; overrides the stored last-notified date"
    )
    parser.add_argument(
        "--all", action="store_true", help="ignore the stored state and send everything matching"
    )
    args = parser.parse_args(argv)

    config = load_config()
    postings = config.curated_dir / "postings.parquet"
    if not postings.exists():
        print("no curated postings; run gradtrack.transform.build first")
        return 1

    frame = pl.read_parquet(postings)
    snapshot = frame["snapshot_date"].max() if frame.height else date.today()

    # Subscribers first. Each has their own role types, family groups and last_notified, so a
    # new subscriber gets everything currently open and everyone else gets only the change.
    store = SubscriptionStore.load(config.manual_dir.parent / STORE_FILE)
    subscribers = store.active()
    if subscribers and not args.dry_run:
        total = 0
        for sub in subscribers:
            since = None if args.all else sub.last_notified
            view = select_for(frame, sub, since)
            messages = render(view, snapshot)
            if not messages:
                continue
            send(config, messages, chat_id=sub.chat_id)
            sub.last_notified = snapshot
            total += view.height
        store.save()
        print(f"sent {total} role(s) across {len(subscribers)} subscriber(s)")
        return 0

    state_path = config.manual_dir / STATE_FILE
    state = NotifyState.load(state_path)
    since = (
        None
        if args.all
        else (date.fromisoformat(args.since) if args.since else state.last_notified)
    )

    view = select_new(frame, since)
    messages = render(view, snapshot)

    if not messages:
        print(f"nothing new since {since}")
        return 0

    if args.dry_run:
        out = config.raw_dir.parent.parent / "reports" / "digest" / f"{snapshot}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n\n---\n\n".join(messages), encoding="utf-8")
        print(f"{view.height} new role(s) in {len(messages)} message(s) -> {out}")
        return 0

    sent = send(config, messages)
    state.save(state_path, snapshot)
    print(f"sent {sent} message(s) covering {view.height} new role(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
