"""Bot interaction logic, driven by synthetic Telegram updates.

The first live attempt looked completely broken — ticks did not move, Done did nothing —
because no process was polling and every tap sat in Telegram's queue. That is a deployment
mistake, not a logic one, but it hid the logic entirely: there was no way to tell a bot that
was not running from a bot whose callbacks were wrong.

So the flow is tested here without Telegram in the loop. Every outbound call is captured, and
the assertions are about what the bot *decided*, not what the network did.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from gradtrack.notify import bot as botmod
from gradtrack.notify.subscriptions import SubscriptionStore

# A made-up id. The real one was pasted in here while the bot was being debugged, and a
# chat id identifies a person — it is why `data/subscriptions.json` is gitignored, and this
# repo is public. Nothing in these tests depends on the value.
CHAT = "100000001"


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """A store on disk plus a recorder standing in for the Bot API."""
    sent: list[tuple[str, dict]] = []

    def fake_call(config, method, **payload):
        sent.append((method, payload))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(botmod, "call", fake_call)
    monkeypatch.setattr(
        botmod, "_say", lambda c, chat, text: sent.append(("sendMessage", {"text": text}))
    )
    monkeypatch.setattr(botmod, "load_postings", lambda config: pl.DataFrame())
    store = SubscriptionStore(path=tmp_path / "subs.json")
    return store, sent


def cb(data: str, message_id: int = 9) -> dict:
    return {
        "id": "1",
        "data": data,
        "message": {"message_id": message_id, "chat": {"id": int(CHAT)}},
    }


class TestMultiSelect:
    def test_tapping_two_options_keeps_both(self, harness) -> None:
        """The reported symptom: only one selection appeared to stick."""
        store, _ = harness
        botmod.handle_callback(None, store, cb("rt:0"))
        botmod.handle_callback(None, store, cb("rt:1"))
        sub = store.get_or_create(CHAT)
        assert sub.role_types == {botmod.ROLE_TYPES[0], botmod.ROLE_TYPES[1]}

    def test_tapping_the_same_option_twice_deselects_it(self, harness) -> None:
        store, _ = harness
        botmod.handle_callback(None, store, cb("rt:0"))
        botmod.handle_callback(None, store, cb("rt:0"))
        assert store.get_or_create(CHAT).role_types == set()

    def test_select_all_and_clear(self, harness) -> None:
        store, _ = harness
        botmod.handle_callback(None, store, cb("rt:all"))
        assert store.get_or_create(CHAT).role_types == set(botmod.ROLE_TYPES)
        botmod.handle_callback(None, store, cb("rt:none"))
        assert store.get_or_create(CHAT).role_types == set()

    def test_selections_survive_a_reload(self, harness) -> None:
        """Every toggle writes the file; a restart must not lose the choices."""
        store, _ = harness
        botmod.handle_callback(None, store, cb("rt:0"))
        reloaded = SubscriptionStore.load(store.path)
        assert reloaded.get_or_create(CHAT).role_types == {botmod.ROLE_TYPES[0]}

    def test_families_are_independent_of_role_types(self, harness) -> None:
        store, _ = harness
        botmod.handle_callback(None, store, cb("rt:0"))
        botmod.handle_callback(None, store, cb("fg:0"))
        sub = store.get_or_create(CHAT)
        assert sub.role_types == {botmod.ROLE_TYPES[0]}
        assert sub.groups == {botmod.GROUPS[0]}


class TestDone:
    def test_done_on_step_one_advances_to_step_two(self, harness) -> None:
        store, sent = harness
        botmod.handle_callback(None, store, cb("rt:0"))
        sent.clear()
        botmod.handle_callback(None, store, cb("rt:done"))
        texts = " ".join(str(p.get("text", "")) for _, p in sent)
        assert "Step 2 of 2" in texts

    def test_done_with_nothing_selected_asks_again(self, harness) -> None:
        """Advancing with an empty selection would subscribe someone to nothing."""
        store, sent = harness
        botmod.handle_callback(None, store, cb("rt:done"))
        texts = " ".join(str(p.get("text", "")) for _, p in sent)
        assert "at least one role type" in texts
        assert "Step 2 of 2" not in texts

    def test_done_on_step_two_saves_and_delivers(self, harness) -> None:
        store, sent = harness
        botmod.handle_callback(None, store, cb("rt:0"))
        botmod.handle_callback(None, store, cb("fg:0"))
        sent.clear()
        botmod.handle_callback(None, store, cb("fg:done"))
        texts = " ".join(str(p.get("text", "")) for _, p in sent)
        assert "Saved" in texts
        assert SubscriptionStore.load(store.path).get_or_create(CHAT).configured

    def test_every_callback_is_answered(self, harness) -> None:
        """An unanswered callback leaves the button spinning, which reads as "does nothing"."""
        store, sent = harness
        for data in ("rt:0", "rt:all", "rt:none", "rt:done", "fg:0", "fg:done"):
            sent.clear()
            botmod.handle_callback(None, store, cb(data))
            assert any(method == "answerCallbackQuery" for method, _ in sent), data

    def test_an_unknown_payload_is_answered_and_ignored(self, harness) -> None:
        store, sent = harness
        botmod.handle_callback(None, store, cb("rt:999"))
        assert store.get_or_create(CHAT).role_types == set()
        assert any(method == "answerCallbackQuery" for method, _ in sent)


class TestCommands:
    def _msg(self, text: str) -> dict:
        return {"chat": {"id": int(CHAT)}, "text": text}

    @pytest.mark.parametrize(
        ("command", "expect"),
        [
            ("/start", "Step 1 of 2"),
            ("/types", "Step 1 of 2"),
            ("/fields", "Step 2 of 2"),
            ("/status", "Role types"),
            ("/stop", "Paused"),
            ("/resume", "Resumed"),
            ("/nonsense", "Singapore Graduate Roles"),
        ],
    )
    def test_commands_respond(self, harness, monkeypatch, command: str, expect: str) -> None:
        store, sent = harness
        monkeypatch.setattr(botmod, "_describe", lambda sub, config: "Role types: none")
        botmod.handle_message(None, store, self._msg(command))
        texts = " ".join(str(p.get("text", "")) for _, p in sent)
        assert expect in texts

    def test_stop_then_resume_round_trips(self, harness) -> None:
        store, _ = harness
        botmod.handle_message(None, store, self._msg("/stop"))
        assert not store.get_or_create(CHAT).active
        botmod.handle_message(None, store, self._msg("/resume"))
        assert store.get_or_create(CHAT).active

    def test_a_command_with_a_bot_suffix_still_works(self, harness) -> None:
        """Group chats deliver "/status@Graduate_Job_Tracker_Bot"."""
        store, sent = harness
        botmod.handle_message(None, store, self._msg("/fields@Graduate_Job_Tracker_Bot"))
        texts = " ".join(str(p.get("text", "")) for _, p in sent)
        assert "Step 2 of 2" in texts

    def test_search_without_a_term_explains_itself(self, harness) -> None:
        store, sent = harness
        botmod.handle_message(None, store, self._msg("/search"))
        texts = " ".join(str(p.get("text", "")) for _, p in sent)
        assert "something to look for" in texts


class TestKeyboard:
    def test_chosen_options_are_ticked(self) -> None:
        markup = botmod._keyboard({botmod.ROLE_TYPES[1]}, botmod.ROLE_TYPES, "rt")
        labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
        assert any(label.startswith("✅") and botmod.ROLE_TYPES[1] in label for label in labels)
        assert any(label.startswith("▫️") and botmod.ROLE_TYPES[0] in label for label in labels)

    def test_callback_payloads_fit_telegrams_limit(self) -> None:
        for options, prefix in ((botmod.ROLE_TYPES, "rt"), (botmod.GROUPS, "fg")):
            markup = botmod._keyboard(set(), options, prefix)
            for row in markup["inline_keyboard"]:
                for button in row:
                    assert len(button["callback_data"].encode()) <= 64

    def test_every_option_has_a_button(self) -> None:
        markup = botmod._keyboard(set(), botmod.GROUPS, "fg", per_row=2)
        buttons = [b for row in markup["inline_keyboard"] for b in row]
        # Every group, plus Select all, Clear and Done.
        assert len(buttons) == len(botmod.GROUPS) + 3


def postings_frame(keys: list[str], seen: date = date(2026, 8, 9)) -> pl.DataFrame:
    n = len(keys)
    return pl.DataFrame(
        {
            "job_key": keys,
            "title": [f"Graduate Analyst {k}" for k in keys],
            "firm_name": ["RWE"] * n,
            "apply_url": [f"https://jobs.rwe.com/RWE/job/Singapore-{k}/" for k in keys],
            "family_group": [botmod.GROUPS[0]] * n,
            "role_type": [botmod.ROLE_TYPES[0]] * n,
            "posted_date": [seen] * n,
            "first_seen": [seen] * n,
            "snapshot_date": [seen] * n,
            "is_grad": [True] * n,
            "is_internship": [False] * n,
            "is_singapore": [True] * n,
            "status": ["open"] * n,
        }
    )


class TestDelivery:
    def _subscribe(self, store):
        sub = store.get_or_create(CHAT)
        sub.role_types = {botmod.ROLE_TYPES[0]}
        sub.groups = {botmod.GROUPS[0]}
        return sub

    def test_the_first_delivery_sends_everything_then_nothing(self, harness, monkeypatch) -> None:
        """First-time-everything and thereafter-only-new are one mechanism."""
        store, _ = harness
        monkeypatch.setattr(botmod, "load_postings", lambda config: postings_frame(["a", "b"]))
        sub = self._subscribe(store)
        assert sub.last_notified is None

        assert botmod.deliver(None, store, sub, since_last=True) == 2
        assert sub.last_notified == date(2026, 8, 9)
        assert botmod.deliver(None, store, sub, since_last=True) == 0

    def test_a_role_added_on_a_date_already_notified_is_still_sent(
        self, harness, monkeypatch
    ) -> None:
        """The bug this replaced a date comparison to fix.

        A second run on the same day — a rebuild, or the slow Workday leg landing after the
        fast one — produces roles whose first_seen equals the date already stamped. "Newer
        than last_notified" skips every one of them and reports nothing new.
        """
        store, _ = harness
        monkeypatch.setattr(botmod, "load_postings", lambda config: postings_frame(["a"]))
        sub = self._subscribe(store)
        assert botmod.deliver(None, store, sub, since_last=True) == 1

        # Same snapshot date, one more role.
        monkeypatch.setattr(botmod, "load_postings", lambda config: postings_frame(["a", "b"]))
        assert botmod.deliver(None, store, sub, since_last=True) == 1

    def test_roles_command_does_not_consume_the_unsent_set(self, harness, monkeypatch) -> None:
        """/roles is a query, not a delivery; it must not silence tomorrow's digest."""
        store, _ = harness
        monkeypatch.setattr(botmod, "load_postings", lambda config: postings_frame(["a", "b"]))
        sub = self._subscribe(store)
        assert botmod.deliver(None, store, sub, since_last=False) == 2
        assert sub.notified_keys == set()
        assert botmod.deliver(None, store, sub, since_last=True) == 2

    def test_closed_roles_are_pruned_from_the_sent_set(self, harness, monkeypatch) -> None:
        """Otherwise the file grows for every role ever seen."""
        store, _ = harness
        monkeypatch.setattr(botmod, "load_postings", lambda config: postings_frame(["a", "b"]))
        sub = self._subscribe(store)
        botmod.deliver(None, store, sub, since_last=True)
        assert sub.notified_keys == {"a", "b"}

        monkeypatch.setattr(botmod, "load_postings", lambda config: postings_frame(["b"]))
        botmod.deliver(None, store, sub, since_last=True)
        assert sub.notified_keys == {"b"}
