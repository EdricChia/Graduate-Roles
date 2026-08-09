"""Per-chat notification preferences.

Stored as JSON at ``data/subscriptions.json``, which is **gitignored**. A Telegram chat id
identifies a person, and this repo is meant to be publishable; the scheduled workflow falls
back to the single chat configured in secrets when the file is absent, so nothing depends on
committing it.

Each subscriber chooses which of the three scope legs they want (graduate programme, graduate
role, entry level) and which family groups. `last_notified` is per-subscriber rather than
global, so a new subscriber gets everything currently open on their first send and only
changes afterwards — the two behaviours the user asked for, from one field.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from gradtrack.schema import PRIORITY_GROUPS, SELECTABLE_ROLE_TYPES

STORE_FILE = "subscriptions.json"


@dataclass
class Subscription:
    """One chat's preferences.

    Attributes:
        chat_id: Telegram chat id, as a string because ids exceed 32 bits.
        role_types: chosen values of :class:`~gradtrack.schema.RoleType`.
        groups: chosen family groups.
        last_notified: the snapshot date of the last digest sent. None means "never sent",
            which is what triggers the full first digest rather than an empty one.
        active: /stop sets this false without discarding the preferences.
    """

    chat_id: str
    role_types: set[str] = field(default_factory=lambda: {t.value for t in SELECTABLE_ROLE_TYPES})
    groups: set[str] = field(default_factory=lambda: set(PRIORITY_GROUPS))
    last_notified: date | None = None
    active: bool = True
    # Which postings this subscriber has already been told about.
    #
    # Selection used to be "first_seen after last_notified", which is wrong whenever a
    # snapshot is rebuilt or the pipeline runs twice in a day: the new roles carry the same
    # first_seen as the date already stamped, so a strict comparison skips them and the run
    # reports nothing new while genuinely new roles sit unsent. Tracking keys says exactly
    # what it means — send what has not been sent — and makes the first send (empty set)
    # and every later send the same code path.
    notified_keys: set[str] = field(default_factory=set)

    @property
    def configured(self) -> bool:
        """Whether this subscriber has anything to receive at all."""
        return bool(self.role_types) and bool(self.groups)

    def to_json(self) -> dict[str, object]:
        return {
            "chat_id": self.chat_id,
            "role_types": sorted(self.role_types),
            "groups": sorted(self.groups),
            "last_notified": self.last_notified.isoformat() if self.last_notified else None,
            "active": self.active,
            "notified_keys": sorted(self.notified_keys),
        }

    @classmethod
    def from_json(cls, raw: dict) -> Subscription:
        stamp = raw.get("last_notified")
        return cls(
            chat_id=str(raw["chat_id"]),
            role_types=set(raw.get("role_types") or []),
            groups=set(raw.get("groups") or []),
            last_notified=date.fromisoformat(stamp) if stamp else None,
            active=bool(raw.get("active", True)),
            notified_keys=set(raw.get("notified_keys") or []),
        )

    def record_sent(self, keys: list[str], live_keys: set[str], snapshot: date) -> None:
        """Mark these as delivered, and forget postings that no longer exist.

        Pruning to what is still in the dataset keeps the file from growing without bound as
        roles close. A key that comes back after being pruned is a repost, and being told
        about it again is the right outcome.
        """
        self.notified_keys = (self.notified_keys | set(keys)) & (live_keys | set(keys))
        self.last_notified = snapshot


@dataclass
class SubscriptionStore:
    """The subscription file, loaded once and written back on every change.

    Rewritten in full each time rather than appended to. The file is a handful of records and
    a partial write that leaves it unparseable would silently stop every notification, which
    is the failure this project spends most of its effort avoiding elsewhere.
    """

    path: Path
    subscriptions: dict[str, Subscription] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> SubscriptionStore:
        target = Path(path)
        if not target.exists():
            return cls(path=target)
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=target)
        subs = {}
        for item in raw.get("subscriptions", []):
            try:
                sub = Subscription.from_json(item)
            except (KeyError, ValueError):
                continue
            subs[sub.chat_id] = sub
        return cls(path=target, subscriptions=subs)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"subscriptions": [s.to_json() for s in self.subscriptions.values()]}
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def get_or_create(self, chat_id: str) -> Subscription:
        chat_id = str(chat_id)
        if chat_id not in self.subscriptions:
            # A brand-new chat starts with nothing selected, so /start walks them through
            # choosing rather than silently assuming defaults they never saw.
            self.subscriptions[chat_id] = Subscription(chat_id, role_types=set(), groups=set())
        return self.subscriptions[chat_id]

    def active(self) -> list[Subscription]:
        return [s for s in self.subscriptions.values() if s.active and s.configured]

    def toggle(self, chat_id: str, bucket: str, value: str) -> Subscription:
        sub = self.get_or_create(chat_id)
        target = sub.role_types if bucket == "role_types" else sub.groups
        if value in target:
            target.discard(value)
        else:
            target.add(value)
        return sub
