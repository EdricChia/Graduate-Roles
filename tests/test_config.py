"""Config loading. Fails closed on an anonymous User-Agent, because a hundred careers hosts
on a twice-daily schedule is exactly the traffic shape that gets an IP blocked."""

from __future__ import annotations

from pathlib import Path

import pytest

from gradtrack.config import FALLBACK_RATE_LIMIT, load_config

VALID = """
[contact]
name = "Test"
email = "test@example.com"

[http]
user_agent = "gradtrack (Test, test@example.com)"

[rate_limits]
default = 1.0
workday = 0.5
"""


def _write(tmp_path: Path, body: str) -> Path:
    target = tmp_path / "config.toml"
    target.write_text(body, encoding="utf-8")
    return target


class TestLoadConfig:
    def test_it_reads_a_valid_file(self, tmp_path: Path) -> None:
        config = load_config(_write(tmp_path, VALID))
        assert config.contact_email == "test@example.com"
        assert config.rate_limit_for("workday") == 0.5

    def test_an_unknown_platform_gets_the_conservative_fallback(self, tmp_path: Path) -> None:
        """Adding a registry row for a new ATS must not require a config edit first."""
        config = load_config(_write(tmp_path, VALID))
        assert config.rate_limit_for("some_new_ats") == FALLBACK_RATE_LIMIT

    def test_a_missing_file_points_at_the_template(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="config.toml.example"):
            load_config(tmp_path / "absent.toml")

    def test_a_user_agent_without_an_email_is_refused(self, tmp_path: Path) -> None:
        body = VALID.replace('"gradtrack (Test, test@example.com)"', '"gradtrack"')
        with pytest.raises(ValueError, match="user_agent"):
            load_config(_write(tmp_path, body))

    def test_telegram_credentials_prefer_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In CI they arrive as repository secrets; there is no config.toml to put them in."""
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from-secret")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
        config = load_config(_write(tmp_path, VALID))
        assert config.telegram_bot_token == "from-secret"
        assert config.telegram_chat_id == "12345"

    def test_paths_resolve_relative_to_the_repo_root(self, tmp_path: Path) -> None:
        config = load_config(_write(tmp_path, VALID))
        assert config.registry_path.is_absolute()
        assert config.registry_path.name == "registry.csv"
