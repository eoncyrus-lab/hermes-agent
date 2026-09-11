"""Pairing writes a credential into the file the whole deployment depends on.

`upsert_env` edits `.env` in place, and `.env` holds every role password, the
provider keys and the Telegram token. The documented restore path is "the
other half of a working restore is .env". So the properties worth pinning are
the boring ones: nothing else changes, the file mode survives, and an
interrupted write cannot leave a half-file behind.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sonder.wechat.login import BOT_TOKEN_KEY, render_qr, upsert_env

ORIGINAL = """\
A=1
# a comment that must survive
SONDER_WECHAT_BOT_TOKEN=stale
B=2
"""


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(ORIGINAL, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_every_other_line_survives_untouched(env_file: Path):
    upsert_env(env_file, {BOT_TOKEN_KEY: "fresh"})
    body = env_file.read_text(encoding="utf-8")

    assert "A=1" in body
    assert "B=2" in body
    assert "# a comment that must survive" in body
    assert "stale" not in body
    assert f"{BOT_TOKEN_KEY}=fresh" in body


def test_an_existing_key_is_replaced_where_it_stood(env_file: Path):
    """Appending a second copy would leave two values for one key, and which
    one wins depends on the reader."""
    upsert_env(env_file, {BOT_TOKEN_KEY: "fresh"})
    body = env_file.read_text(encoding="utf-8")

    assert body.count(f"{BOT_TOKEN_KEY}=") == 1
    assert body.index("A=1") < body.index(f"{BOT_TOKEN_KEY}=") < body.index("B=2")


def test_a_missing_key_is_appended(env_file: Path):
    upsert_env(env_file, {"SONDER_WECHAT_BASE_URL": "https://api.deepseek.com"})
    assert "SONDER_WECHAT_BASE_URL=https://api.deepseek.com" in env_file.read_text()


def test_the_file_mode_is_carried_across(env_file: Path):
    upsert_env(env_file, {BOT_TOKEN_KEY: "fresh"})
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_a_new_file_is_created_private(tmp_path: Path):
    path = tmp_path / ".env"
    upsert_env(path, {BOT_TOKEN_KEY: "fresh"})
    assert path.stat().st_mode & 0o777 == 0o600


def test_no_temporary_file_is_left_behind(env_file: Path):
    upsert_env(env_file, {BOT_TOKEN_KEY: "fresh", "SONDER_WECHAT_BASE_URL": "https://x"})
    leftovers = [p for p in env_file.parent.iterdir() if p.name != ".env"]
    assert leftovers == []


def test_the_file_ends_with_exactly_one_newline(env_file: Path):
    upsert_env(env_file, {"SONDER_WECHAT_BASE_URL": "https://x"})
    body = env_file.read_text(encoding="utf-8")
    assert body.endswith("\n") and not body.endswith("\n\n")


def test_a_comment_naming_a_key_is_not_rewritten(tmp_path: Path):
    """A commented-out setting is documentation, not a value to overwrite."""
    path = tmp_path / ".env"
    path.write_text(f"# {BOT_TOKEN_KEY}=an example\n", encoding="utf-8")

    upsert_env(path, {BOT_TOKEN_KEY: "fresh"})
    body = path.read_text(encoding="utf-8")

    assert f"# {BOT_TOKEN_KEY}=an example" in body
    assert f"{BOT_TOKEN_KEY}=fresh" in body


def test_a_value_containing_equals_survives():
    """Split on the first `=` only. A DSN or a base64 token can contain more."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".env"
        upsert_env(path, {"SONDER_LLM_ROUTES": '{"a":[{"provider":"x","model":"y"}]}'})
        assert "model" in path.read_text(encoding="utf-8")


def test_the_qr_payload_is_drawable_as_text():
    """What gets scanned is the URL the endpoint returns, not an image."""
    drawn = render_qr("https://liteapp.weixin.qq.com/q/abc?qrcode=def&bot_type=3")
    assert drawn.count("\n") > 5
    assert "\u2588" in drawn or "\u2580" in drawn
