"""Interactive Telegram bot: pick what you want, then get only that.

    uv run python -m gradtrack.notify.bot          # long-poll until interrupted
    uv run python -m gradtrack.notify.bot --once   # drain pending updates and exit

**The bot has to be running for its buttons to do anything.** Telegram queues callbacks and
delivers them to whoever next calls `getUpdates`; with no process polling, a tapped button
looks broken — the tick does not move, Done does nothing — while the taps pile up server
side. That is not a hypothesis: the first live attempt left eight queued updates, four of
them repeated presses of Done. `--once` drains the queue and exits, which is right for cron
and wrong for someone tapping buttons.

Long polling rather than a webhook, because a webhook needs a public HTTPS endpoint and this
is a personal tracker.

The flow:

1. `/start` offers the three scope legs as a multi-select keyboard.
2. Then the family groups, also multi-select, two per row.
3. On **Done**, the subscriber receives everything currently open that matches.
4. From then on the scheduled digest sends only what is new to them.

Steps 3 and 4 are one mechanism: `last_notified` starts null, "everything since null" means
everything, and the send stamps it.
"""

from __future__ import annotations

import argparse
import contextlib
import time
from typing import Any

import httpx
import polars as pl

from gradtrack.config import Config, load_config
from gradtrack.notify.subscriptions import STORE_FILE, Subscription, SubscriptionStore
from gradtrack.notify.telegram import render, select_for
from gradtrack.schema import FAMILY_GROUPS, PRIORITY_GROUPS, SELECTABLE_ROLE_TYPES

API = "https://api.telegram.org/bot{token}/{method}"
POLL_TIMEOUT = 50
LATEST_DEFAULT = 15
# Consecutive Conflict responses before giving up. Telegram allows one long poll per
# token, and a conflict never resolves on its own.
MAX_CONFLICTS = 3

# Callback payloads are capped at 64 bytes, so buttons carry an index into these tuples.
ROLE_TYPES: tuple[str, ...] = tuple(t.value for t in SELECTABLE_ROLE_TYPES)
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(FAMILY_GROUPS.values()))

COMMANDS: list[tuple[str, str]] = [
    ("start", "Set up what you want to hear about"),
    ("types", "Change role types (programme / graduate / entry level)"),
    ("fields", "Change which job families you follow"),
    ("roles", "Everything currently open that matches you"),
    ("latest", "The newest roles, e.g. /latest 20"),
    ("search", "Find roles by keyword, e.g. /search quant"),
    ("firms", "Which firms have open roles for you"),
    ("status", "Your current settings"),
    ("stop", "Pause notifications"),
    ("resume", "Resume notifications"),
    ("help", "Show this list"),
]

HELP = (
    "<b>Singapore Graduate Roles</b>\n\n"
    + "\n".join(f"/{name} — {desc}" for name, desc in COMMANDS)
    + "\n\nEvery link goes to the firm's own careers page."
)


def call(config: Config, method: str, **payload: Any) -> dict:
    """Call the Bot API. Raises on a genuine failure, tolerates the harmless ones."""
    with httpx.Client(timeout=POLL_TIMEOUT + 15) as client:
        response = client.post(
            API.format(token=config.telegram_bot_token, method=method), json=payload
        )
    try:
        body = response.json()
    except ValueError:
        body = {"ok": False, "description": response.text[:200]}
    if not body.get("ok"):
        description = str(body.get("description", ""))
        # Editing a message to exactly what it already says is an error to Telegram and a
        # no-op to us. It happens whenever a toggle lands on the state already displayed —
        # tapping Clear on an empty selection, or a duplicate tap — and it must not look
        # like a failure.
        if "message is not modified" in description.lower():
            return {"ok": True, "result": None}
        raise RuntimeError(f"Telegram {method} failed: {description}")
    return body


def register_commands(config: Config) -> None:
    """Publish the command list so Telegram shows a menu button.

    Without this the commands work but are invisible, and the only way to learn them is to
    read the help text — which you have to know to ask for.
    """
    call(
        config,
        "setMyCommands",
        commands=[{"command": name, "description": desc} for name, desc in COMMANDS],
    )


def _keyboard(chosen: set[str], options: tuple[str, ...], prefix: str, per_row: int = 1) -> dict:
    """A multi-select keyboard: a tick marks what is already chosen."""
    buttons = [
        {
            "text": f"{'✅' if option in chosen else '▫️'} {option}",
            "callback_data": f"{prefix}:{index}",
        }
        for index, option in enumerate(options)
    ]
    rows = [buttons[i : i + per_row] for i in range(0, len(buttons), per_row)]
    rows.append(
        [
            {"text": "Select all", "callback_data": f"{prefix}:all"},
            {"text": "Clear", "callback_data": f"{prefix}:none"},
        ]
    )
    rows.append([{"text": "✔️  Done", "callback_data": f"{prefix}:done"}])
    return {"inline_keyboard": rows}


def _prompt(config: Config, chat_id: str, text: str, markup: dict, edit: int | None) -> None:
    if edit:
        call(
            config,
            "editMessageText",
            chat_id=chat_id,
            message_id=edit,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )
    else:
        call(
            config,
            "sendMessage",
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            reply_markup=markup,
        )


def send_role_type_prompt(
    config: Config, chat_id: str, sub: Subscription, edit: int | None = None
) -> None:
    chosen = len(sub.role_types)
    text = (
        f"<b>Step 1 of 2 — what kind of role?</b>  ({chosen} selected)\n"
        "Tap to toggle as many as you like, then Done.\n\n"
        "<b>Graduate programme</b> — named schemes "
        "(Point72 Academy, RWE Graduate Programme, GOglobal)\n"
        "<b>Graduate role</b> — says fresh grad / final year / campus, not a named scheme\n"
        "<b>Entry level</b> — the employer's own field says zero years or entry level"
    )
    _prompt(config, chat_id, text, _keyboard(sub.role_types, ROLE_TYPES, "rt"), edit)


def send_group_prompt(
    config: Config, chat_id: str, sub: Subscription, edit: int | None = None
) -> None:
    chosen = len(sub.groups)
    text = (
        f"<b>Step 2 of 2 — which fields?</b>  ({chosen} selected)\n"
        "Tap to toggle, then Done.\n\n"
        f"<i>Suggested: {', '.join(sorted(PRIORITY_GROUPS))}</i>"
    )
    _prompt(config, chat_id, text, _keyboard(sub.groups, GROUPS, "fg", per_row=2), edit)


def load_postings(config: Config) -> pl.DataFrame:
    path = config.curated_dir / "postings.parquet"
    return pl.read_parquet(path) if path.exists() else pl.DataFrame()


def _say(config: Config, chat_id: str, text: str) -> None:
    call(
        config,
        "sendMessage",
        chat_id=chat_id,
        text=text,
        parse_mode="HTML",
        disable_web_page_preview=True,
    )


def deliver(
    config: Config, store: SubscriptionStore, sub: Subscription, *, since_last: bool
) -> int:
    """Send this subscriber their matching roles. Returns how many were sent."""
    frame = load_postings(config)
    if frame.is_empty():
        _say(config, sub.chat_id, "No data yet — the tracker has not run.")
        return 0

    view = select_for(frame, sub, since=sub.last_notified if since_last else None)
    snapshot = frame["snapshot_date"].max()
    if view.is_empty():
        if not since_last:
            _say(
                config,
                sub.chat_id,
                "Nothing open matches those choices right now. /types or /fields to widen.",
            )
        return 0

    for message in render(view, snapshot):
        _say(config, sub.chat_id, message)
    sub.last_notified = snapshot
    store.save()
    return view.height


def _describe(sub: Subscription, config: Config) -> str:
    frame = load_postings(config)
    matching = 0 if frame.is_empty() else select_for(frame, sub, None).height
    return (
        f"<b>Role types:</b> {', '.join(sorted(sub.role_types)) or '<i>none</i>'}\n"
        f"<b>Fields:</b> {', '.join(sorted(sub.groups)) or '<i>none</i>'}\n"
        f"<b>Notifications:</b> {'on' if sub.active else 'paused'}\n"
        f"<b>Open roles matching you:</b> {matching}"
    )


def _rows_message(view: pl.DataFrame, heading: str) -> list[str]:
    if view.is_empty():
        return [f"{heading}\n\nNothing matches."]
    lines = [heading]
    for row in view.iter_rows(named=True):
        when = row["posted_date"] or row["first_seen"]
        lines.append(
            f'• <a href="{row["apply_url"]}">{row["title"][:80]}</a>\n'
            f"  {row['firm_name'][:38]} · {when.isoformat() if when else 'date unknown'}"
        )
    # Reuse the digest's chunking rules by keeping messages short here.
    out, current = [], []
    size = 0
    for line in lines:
        if size + len(line) > 3500 and current:
            out.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    out.append("\n".join(current))
    return out


def handle_message(config: Config, store: SubscriptionStore, message: dict) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    raw = (message.get("text") or "").strip()
    if not chat_id:
        return
    command, _, argument = raw.partition(" ")
    command = command.lower().split("@")[0]
    sub = store.get_or_create(chat_id)
    frame = load_postings(config)

    if command == "/start":
        sub.active = True
        _say(
            config,
            chat_id,
            "👋 I track graduate openings in Singapore at large, well-paying firms.\n"
            "Two quick questions, then you get everything currently open.",
        )
        send_role_type_prompt(config, chat_id, sub)
    elif command in {"/types", "/prefs"}:
        send_role_type_prompt(config, chat_id, sub)
    elif command == "/fields":
        send_group_prompt(config, chat_id, sub)
    elif command == "/roles":
        sent = deliver(config, store, sub, since_last=False)
        if sent:
            _say(config, chat_id, f"That is {sent} open role(s) matching your settings.")
    elif command == "/latest":
        try:
            limit = max(1, min(50, int(argument)))
        except ValueError:
            limit = LATEST_DEFAULT
        view = select_for(frame, sub, None).head(limit) if not frame.is_empty() else pl.DataFrame()
        for chunk in _rows_message(view, f"<b>Newest {limit} matching role(s)</b>"):
            _say(config, chat_id, chunk)
    elif command == "/search":
        if not argument.strip():
            _say(config, chat_id, "Give me something to look for, e.g. <code>/search quant</code>")
        else:
            needle = argument.strip()
            view = (
                select_for(frame, sub, None).filter(
                    pl.col("title").str.contains(f"(?i){needle}")
                    | pl.col("firm_name").str.contains(f"(?i){needle}")
                )
                if not frame.is_empty()
                else pl.DataFrame()
            )
            for chunk in _rows_message(view.head(40), f"<b>Matches for “{needle}”</b>"):
                _say(config, chat_id, chunk)
    elif command == "/firms":
        if frame.is_empty():
            _say(config, chat_id, "No data yet.")
        else:
            counts = (
                select_for(frame, sub, None)
                .group_by("firm_name")
                .len()
                .sort("len", descending=True)
            )
            body = "\n".join(f"{n:>3}  {name}" for name, n in counts.iter_rows()) or "None."
            _say(config, chat_id, f"<b>Firms with open roles for you</b>\n<pre>{body}</pre>")
    elif command == "/status":
        _say(config, chat_id, _describe(sub, config))
    elif command == "/stop":
        sub.active = False
        _say(config, chat_id, "Paused. Your settings are kept — /resume when you want them back.")
    elif command == "/resume":
        sub.active = True
        _say(config, chat_id, "Resumed.")
    else:
        _say(config, chat_id, HELP)
    store.save()


def handle_callback(config: Config, store: SubscriptionStore, callback: dict) -> None:
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")
    # Always answer, even on a payload we cannot act on. An unanswered callback leaves the
    # button spinning in the client, which is what "the button does nothing" looks like.
    with contextlib.suppress(RuntimeError, httpx.HTTPError):
        call(config, "answerCallbackQuery", callback_query_id=callback["id"])
    if not chat_id or ":" not in data:
        return

    prefix, action = data.split(":", 1)
    options = ROLE_TYPES if prefix == "rt" else GROUPS
    sub = store.get_or_create(chat_id)
    target = sub.role_types if prefix == "rt" else sub.groups

    if action == "all":
        target.clear()
        target.update(options)
    elif action == "none":
        target.clear()
    elif action == "done":
        store.save()
        if prefix == "rt":
            if not sub.role_types:
                _say(config, chat_id, "Pick at least one role type first.")
                send_role_type_prompt(config, chat_id, sub)
                return
            send_group_prompt(config, chat_id, sub)
            return
        if not sub.groups:
            _say(config, chat_id, "Pick at least one field first.")
            send_group_prompt(config, chat_id, sub)
            return
        _say(config, chat_id, f"Saved.\n\n{_describe(sub, config)}\n\nHere is everything open:")
        if not deliver(config, store, sub, since_last=True):
            _say(config, chat_id, "Nothing matches yet — I will tell you when something does.")
        return
    else:
        # A stale keyboard from an older deploy can send an index that no longer exists.
        with contextlib.suppress(ValueError, IndexError):
            target.symmetric_difference_update({options[int(action)]})

    store.save()
    if prefix == "rt":
        send_role_type_prompt(config, chat_id, sub, edit=message_id)
    else:
        send_group_prompt(config, chat_id, sub, edit=message_id)


def poll(config: Config, store: SubscriptionStore, *, once: bool) -> int:
    offset: int | None = None
    conflicts = 0
    while True:
        try:
            body = call(config, "getUpdates", offset=offset, timeout=0 if once else POLL_TIMEOUT)
            conflicts = 0
        except (RuntimeError, httpx.HTTPError) as exc:
            # Telegram allows exactly one long poll per token. A second instance makes both
            # fail with Conflict, and neither can ever recover — so retrying forever just
            # prints the same line every few seconds while every button in the chat stays
            # dead. This happened for real: a bot left running by an earlier session fought
            # a new one, and the symptom was indistinguishable from no bot at all.
            if "conflict" in str(exc).lower():
                conflicts += 1
                if conflicts >= MAX_CONFLICTS:
                    print(
                        "\nAnother instance of this bot is already polling.\n"
                        "Telegram permits one getUpdates connection per token, so both are\n"
                        "now broken. Stop the other process and start this one again:\n"
                        "  pgrep -af 'gradtrack.notify.bot'      # or Get-CimInstance "
                        "Win32_Process on Windows\n"
                    )
                    return 1
            print(f"poll failed: {exc}")
            if once:
                return 1
            time.sleep(5)
            continue

        updates = body.get("result", [])
        for update in updates:
            offset = update["update_id"] + 1
            try:
                if "message" in update:
                    handle_message(config, store, update["message"])
                elif "callback_query" in update:
                    handle_callback(config, store, update["callback_query"])
            except Exception as exc:  # noqa: BLE001 - one bad update must not stop the bot
                print(f"update {update.get('update_id')} failed: {type(exc).__name__}: {exc}")
        if updates:
            print(f"handled {len(updates)} update(s)")
        if once:
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interactive Telegram bot")
    parser.add_argument("--once", action="store_true", help="drain pending updates and exit")
    args = parser.parse_args(argv)

    config = load_config()
    if not config.telegram_bot_token:
        print("TELEGRAM_BOT_TOKEN is unset")
        return 1
    store = SubscriptionStore.load(config.manual_dir.parent / STORE_FILE)
    with contextlib.suppress(RuntimeError, httpx.HTTPError):
        register_commands(config)
    print(f"bot polling ({len(store.subscriptions)} known chat(s)); Ctrl-C to stop")
    try:
        return poll(config, store, once=args.once)
    except KeyboardInterrupt:
        store.save()
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
