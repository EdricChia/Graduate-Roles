"""Interactive Telegram bot: pick what you want, then get only that.

    uv run python -m gradtrack.notify.bot          # long-poll until interrupted
    uv run python -m gradtrack.notify.bot --once   # drain pending updates and exit

Long polling rather than a webhook, because a webhook needs a public HTTPS endpoint and this
is a personal tracker that runs from a laptop and a scheduled job. `--once` exists so the
same code can be driven from cron: it drains whatever arrived since last time and exits.

The flow the user asked for:

1. `/start` offers the three scope legs — graduate programme, graduate role, entry level —
   as a multi-select keyboard.
2. Then the family groups, also multi-select.
3. On **Done**, the subscriber immediately receives everything currently open that matches.
4. From then on the scheduled digest sends only what is new to them.

Steps 3 and 4 are one mechanism, not two: a subscriber's `last_notified` starts null, "send
everything since last_notified" with a null means "send everything", and the send stamps it.

Selections are toggles rather than a wizard, because the answer to "which families?" is
usually several and a one-question-at-a-time flow makes that tedious.
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

# Callback payloads are capped at 64 bytes, so the buttons carry an index into these tuples
# rather than the label itself.
ROLE_TYPES: tuple[str, ...] = tuple(t.value for t in SELECTABLE_ROLE_TYPES)
GROUPS: tuple[str, ...] = tuple(dict.fromkeys(FAMILY_GROUPS.values()))

HELP = (
    "<b>Singapore Graduate Roles</b>\n\n"
    "/start — choose what you want to hear about\n"
    "/prefs — change your choices\n"
    "/roles — resend everything currently open that matches\n"
    "/status — what you are subscribed to\n"
    "/stop — pause notifications (your choices are kept)\n\n"
    "Every link goes to the firm's own careers page."
)


def call(config: Config, method: str, **payload: Any) -> dict:
    with httpx.Client(timeout=POLL_TIMEOUT + 15) as client:
        response = client.post(
            API.format(token=config.telegram_bot_token, method=method), json=payload
        )
    try:
        body = response.json()
    except ValueError:
        body = {"ok": False, "description": response.text[:200]}
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {body.get('description')}")
    return body


def _keyboard(chosen: set[str], options: tuple[str, ...], prefix: str) -> dict:
    """A multi-select keyboard: a tick marks what is already chosen."""
    rows = [
        [
            {
                "text": f"{'✅' if option in chosen else '▫️'} {option}",
                "callback_data": f"{prefix}:{index}",
            }
        ]
        for index, option in enumerate(options)
    ]
    rows.append(
        [
            {"text": "Select all", "callback_data": f"{prefix}:all"},
            {"text": "Clear", "callback_data": f"{prefix}:none"},
        ]
    )
    rows.append([{"text": "➡️  Done", "callback_data": f"{prefix}:done"}])
    return {"inline_keyboard": rows}


def send_role_type_prompt(config: Config, chat_id: str, sub: Subscription, edit: int | None = None):
    text = (
        "<b>Step 1 of 2 — what kind of role?</b>\n"
        "Tap to toggle. You can pick more than one.\n\n"
        "<b>Graduate programme</b> — named schemes "
        "(Point72 Academy, GOglobal, Management Associate)\n"
        "<b>Graduate role</b> — says fresh grad / final year / campus, but is not a named scheme\n"
        "<b>Entry level</b> — the employer's structured field says zero years or entry level"
    )
    markup = _keyboard(sub.role_types, ROLE_TYPES, "rt")
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


def send_group_prompt(config: Config, chat_id: str, sub: Subscription, edit: int | None = None):
    starred = ", ".join(sorted(PRIORITY_GROUPS))
    text = (
        "<b>Step 2 of 2 — which fields?</b>\n"
        "Tap to toggle, then Done.\n\n"
        f"<i>Suggested: {starred}</i>"
    )
    markup = _keyboard(sub.groups, GROUPS, "fg")
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


def load_postings(config: Config) -> pl.DataFrame:
    path = config.curated_dir / "postings.parquet"
    return pl.read_parquet(path) if path.exists() else pl.DataFrame()


def deliver(
    config: Config, store: SubscriptionStore, sub: Subscription, *, since_last: bool
) -> int:
    """Send this subscriber their matching roles. Returns how many were sent.

    ``since_last=False`` is /roles — everything currently open. ``True`` is the scheduled
    path, and on a subscriber whose ``last_notified`` is still null it sends everything too,
    which is exactly the first-time behaviour.
    """
    frame = load_postings(config)
    if frame.is_empty():
        call(
            config,
            "sendMessage",
            chat_id=sub.chat_id,
            text="No data yet — the tracker has not run.",
        )
        return 0

    view = select_for(frame, sub, since=sub.last_notified if since_last else None)
    snapshot = frame["snapshot_date"].max()
    if view.is_empty():
        if not since_last:
            call(
                config,
                "sendMessage",
                chat_id=sub.chat_id,
                text="Nothing open matches those choices right now. /prefs to widen them.",
            )
        return 0

    for message in render(view, snapshot):
        call(
            config,
            "sendMessage",
            chat_id=sub.chat_id,
            text=message,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    sub.last_notified = snapshot
    store.save()
    return view.height


def _describe(sub: Subscription) -> str:
    return (
        f"<b>Role types:</b> {', '.join(sorted(sub.role_types)) or 'none'}\n"
        f"<b>Fields:</b> {', '.join(sorted(sub.groups)) or 'none'}\n"
        f"<b>Active:</b> {'yes' if sub.active else 'no (paused)'}"
    )


def handle_message(config: Config, store: SubscriptionStore, message: dict) -> None:
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip().lower()
    if not chat_id:
        return
    sub = store.get_or_create(chat_id)

    if text.startswith("/start"):
        sub.active = True
        call(
            config,
            "sendMessage",
            chat_id=chat_id,
            parse_mode="HTML",
            text="👋 I track graduate openings in Singapore at large, well-paying firms.\n"
            "Two quick questions, then you will get everything currently open.",
        )
        send_role_type_prompt(config, chat_id, sub)
    elif text.startswith("/prefs"):
        send_role_type_prompt(config, chat_id, sub)
    elif text.startswith("/roles"):
        sent = deliver(config, store, sub, since_last=False)
        if sent:
            call(config, "sendMessage", chat_id=chat_id, text=f"Sent {sent} matching role(s).")
    elif text.startswith("/status"):
        call(config, "sendMessage", chat_id=chat_id, text=_describe(sub), parse_mode="HTML")
    elif text.startswith("/stop"):
        sub.active = False
        call(
            config,
            "sendMessage",
            chat_id=chat_id,
            text="Paused. Your choices are kept — /start to resume.",
        )
    else:
        call(config, "sendMessage", chat_id=chat_id, text=HELP, parse_mode="HTML")
    store.save()


def handle_callback(config: Config, store: SubscriptionStore, callback: dict) -> None:
    data = callback.get("data") or ""
    message = callback.get("message") or {}
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")
    if not chat_id or ":" not in data:
        return

    prefix, action = data.split(":", 1)
    bucket, options = ("role_types", ROLE_TYPES) if prefix == "rt" else ("groups", GROUPS)
    sub = store.get_or_create(chat_id)
    target = sub.role_types if bucket == "role_types" else sub.groups

    if action == "all":
        target.clear()
        target.update(options)
    elif action == "none":
        target.clear()
    elif action == "done":
        store.save()
        call(config, "answerCallbackQuery", callback_query_id=callback["id"])
        if prefix == "rt":
            if not sub.role_types:
                call(
                    config,
                    "sendMessage",
                    chat_id=chat_id,
                    text="Pick at least one role type first.",
                )
                send_role_type_prompt(config, chat_id, sub)
                return
            send_group_prompt(config, chat_id, sub)
            return
        if not sub.groups:
            call(config, "sendMessage", chat_id=chat_id, text="Pick at least one field first.")
            send_group_prompt(config, chat_id, sub)
            return
        call(
            config,
            "sendMessage",
            chat_id=chat_id,
            parse_mode="HTML",
            text=f"Saved.\n\n{_describe(sub)}\n\nHere is everything open that matches:",
        )
        # First delivery: last_notified is still null, so this sends the full current set and
        # stamps the date. Every later send is the difference.
        sent = deliver(config, store, sub, since_last=True)
        if not sent:
            call(
                config,
                "sendMessage",
                chat_id=chat_id,
                text="Nothing matches yet — you will hear from me when something does.",
            )
        return
    else:
        # A stale keyboard from an older deploy can send an index that no longer exists.
        with contextlib.suppress(ValueError, IndexError):
            target.symmetric_difference_update({options[int(action)]})

    store.save()
    call(config, "answerCallbackQuery", callback_query_id=callback["id"])
    if prefix == "rt":
        send_role_type_prompt(config, chat_id, sub, edit=message_id)
    else:
        send_group_prompt(config, chat_id, sub, edit=message_id)


def poll(config: Config, store: SubscriptionStore, *, once: bool) -> int:
    offset: int | None = None
    while True:
        try:
            body = call(config, "getUpdates", offset=offset, timeout=0 if once else POLL_TIMEOUT)
        except (RuntimeError, httpx.HTTPError) as exc:
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
        if once:
            print(f"processed {len(updates)} update(s)")
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
    print(f"bot running ({len(store.subscriptions)} known chat(s)); Ctrl-C to stop")
    try:
        return poll(config, store, once=args.once)
    except KeyboardInterrupt:
        store.save()
        print("\nstopped")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
