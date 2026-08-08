"""Loader for the project's ``config.toml``.

Kept outside ``sources/`` and ``ingest/`` so that a client module never has to reach for the
filesystem to answer "what is my User-Agent". The client takes a ``Config`` object as an
argument; the read happens once, here.

``config.toml`` is gitignored. ``config.toml.example`` is the committed template.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# src/gradtrack/config.py -> src/gradtrack -> src -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = REPO_ROOT / "config.toml"
EXAMPLE_PATH = REPO_ROOT / "config.toml.example"

# Applied when a platform has no explicit entry in [rate_limits]. One request per second is
# the floor for any careers host we have not specifically reasoned about.
FALLBACK_RATE_LIMIT = 1.0


@dataclass(frozen=True)
class Config:
    """Everything an ingest job needs that is not in code.

    Attributes:
        user_agent: sent on every outbound request. A descriptive string with a working
            contact email is the difference between a polite crawler and a blocked IP, and
            we are hitting several hundred careers hosts on a schedule.
        contact_email: the email inside ``user_agent``, kept separately so it can be
            asserted on.
        rate_limits: requests per second, keyed by platform name as it appears in the firm
            registry's ``ats_platform`` column. Missing keys fall back to
            ``FALLBACK_RATE_LIMIT``.
        raw_dir: root of the append-only raw snapshot tree.
        manual_dir: root of the hand-keyed CSV tree.
        firms_dir: holds ``registry.csv``, the firm list that drives every ingest run.
        curated_dir: parquet outputs of the transform step.
        duckdb_path: the curated DuckDB database file.
        telegram_bot_token: empty unless notifying. CI supplies it from a secret.
        telegram_chat_id: as above.
    """

    user_agent: str
    contact_email: str
    raw_dir: Path
    manual_dir: Path
    firms_dir: Path
    curated_dir: Path
    duckdb_path: Path
    rate_limits: dict[str, float] = field(default_factory=dict)
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    def rate_limit_for(self, platform: str) -> float:
        """Requests per second allowed against ``platform``.

        Unknown platforms get the conservative fallback rather than an error, so adding a
        registry row for a new ATS does not require a config edit before it can run.
        """
        return float(self.rate_limits.get(platform, FALLBACK_RATE_LIMIT))

    @property
    def registry_path(self) -> Path:
        return self.firms_dir / "registry.csv"


def load_config(path: Path | str | None = None) -> Config:
    """Read ``config.toml`` and return it as a ``Config``.

    Telegram credentials prefer the environment (``TELEGRAM_BOT_TOKEN`` /
    ``TELEGRAM_CHAT_ID``) over the file, because in CI they arrive as repository secrets and
    there is no ``config.toml`` section to put them in.

    Raises:
        FileNotFoundError: if ``config.toml`` is absent. The message points at the template,
            because this is the first thing a fresh clone hits.
        ValueError: if the User-Agent is missing or carries no email address. An anonymous
            request is what gets a careers host to block us, so this fails closed.
    """
    target = Path(path) if path is not None else CONFIG_PATH
    if not target.exists():
        raise FileNotFoundError(
            f"{target} not found. Copy {EXAMPLE_PATH.name} to config.toml and fill in a "
            "working contact email before running any ingest job."
        )

    with target.open("rb") as handle:
        raw = tomllib.load(handle)

    user_agent = str(raw.get("http", {}).get("user_agent", "")).strip()
    contact_email = str(raw.get("contact", {}).get("email", "")).strip()
    if not user_agent or "@" not in user_agent:
        raise ValueError(
            f"{target}: [http].user_agent must be a descriptive string containing a contact "
            f"email; got {user_agent!r}"
        )
    if "@" not in contact_email:
        raise ValueError(f"{target}: [contact].email must be an email address")

    paths = raw.get("paths", {})
    telegram = raw.get("telegram", {})

    def _path(key: str, fallback: str) -> Path:
        value = Path(str(paths.get(key, fallback)))
        return value if value.is_absolute() else REPO_ROOT / value

    return Config(
        user_agent=user_agent,
        contact_email=contact_email,
        rate_limits={k: float(v) for k, v in raw.get("rate_limits", {}).items()},
        raw_dir=_path("raw", "data/raw"),
        manual_dir=_path("manual", "data/manual"),
        firms_dir=_path("firms", "data/firms"),
        curated_dir=_path("curated", "data/curated"),
        duckdb_path=_path("duckdb", "data/curated.duckdb"),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", str(telegram.get("bot_token", ""))),
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", str(telegram.get("chat_id", ""))),
    )
