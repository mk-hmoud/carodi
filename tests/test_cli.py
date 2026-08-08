from __future__ import annotations

from pathlib import Path

import pytest

from carodi.cli import main
from carodi.pipeline import Funnel

CONFIG_DIR = str(Path(__file__).resolve().parent.parent / "config")


@pytest.fixture(autouse=True)
def no_telegram_credentials(monkeypatch):
    monkeypatch.delenv("CARODI_TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("CARODI_TELEGRAM_CHAT_ID", raising=False)


def test_missing_credentials_are_caught_before_any_source_is_fetched(monkeypatch):
    """Regression: the funnel swept every source for ~30s and only then
    discovered there was nowhere to deliver the result."""
    fetched = []
    monkeypatch.setattr(Funnel, "run", lambda self, dry_run=False: fetched.append(1))

    assert main(["-c", CONFIG_DIR, "run"]) == 1
    assert fetched == [], "sources were fetched despite unusable credentials"


def test_misconfiguration_is_reported_as_one_line_not_a_traceback(capsys):
    assert main(["-c", CONFIG_DIR, "run"]) == 1
    err = capsys.readouterr().err
    assert "error: Telegram sink needs both a bot token and a chat id" in err
    assert "Traceback" not in err


def test_missing_config_directory_exits_cleanly(capsys):
    assert main(["-c", "/nonexistent/config", "run"]) == 1
    err = capsys.readouterr().err
    assert "missing config file" in err
    assert "Traceback" not in err


def test_dry_run_needs_no_credentials(monkeypatch, tmp_path):
    """--dry-run uses the console sink, so it must work before Telegram exists."""
    # db_path in the config is relative, and the store is opened even for a dry
    # run -- without this the test writes data/carodi.db into the working tree.
    monkeypatch.chdir(tmp_path)

    ran = []
    monkeypatch.setattr(Funnel, "run", lambda self, dry_run=False: ran.append(dry_run) or _Empty())

    assert main(["-c", CONFIG_DIR, "run", "--dry-run"]) == 0
    assert ran == [True]
    assert (tmp_path / "data" / "carodi.db").exists()


class _Empty:
    """Minimal stand-in for RunResult."""

    fetched = after_dedupe = passed = 0
    new: list = []
    source_errors: dict = {}

    def __init__(self):
        from collections import Counter

        self.rejections = Counter()
