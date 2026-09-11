"""Pairing a WeChat bot account, and remembering what the pairing returns.

Three steps: ask Tencent for a code, wait for a human to scan it, keep the
token that comes back. The token can send messages as the bot, so it is
handled the way `SONDER_TELEGRAM_TOKEN` already is -- written to `.env`, mode
0600, never printed and never logged.

It deliberately does **not** go in the vault. `vault_records` holds sensitive
material *about a person* -- every value of its `provenance` enum is a
statement by somebody -- it is scoped by `relationship_id`, and `sonder_app`
holds no grant on it whatsoever. A channel credential is an operator secret,
which is exactly what `.env` is for.
"""

from __future__ import annotations

import io
import os
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path

import qrcode
import structlog
from qrcode.image.svg import SvgPathImage

from .client import LoginQrcode, LoginResult, request_login_qrcode, wait_for_login

log = structlog.get_logger(__name__)

BOT_TOKEN_KEY = "SONDER_WECHAT_BOT_TOKEN"
BASE_URL_KEY = "SONDER_WECHAT_BASE_URL"


def render_qr(payload: str) -> str:
    """The QR as text.

    What gets scanned is the `qrcode_img_content` URL, not an image, and the
    operator is sitting at a terminal when they pair -- so the terminal is
    where it has to be drawable. `print_ascii` is pure Python: no Pillow, no
    system libraries.
    """
    code = qrcode.QRCode(border=1)
    code.add_data(payload)
    code.make(fit=True)
    buffer = io.StringIO()
    code.print_ascii(out=buffer, invert=True)
    return buffer.getvalue()


def upsert_env(path: Path, values: Mapping[str, str]) -> None:
    """Set keys in `.env`, leaving every other line exactly as it was.

    Atomic, and that is not a nicety: this file holds every role password, the
    provider keys and the Telegram token, and the documented restore path is
    "the other half of a working restore is .env". A run interrupted halfway
    through a rewrite would take the deployment with it, so the new content is
    written to a sibling and renamed over the original in one step.

    The file mode is carried across rather than reset, because whoever created
    it may have had a reason -- and if it did not exist, 0600 is the right
    default for a file full of credentials.
    """
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    remaining = dict(values)
    out: list[str] = []
    for line in existing:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                out.append(f"{key}={remaining.pop(key)}")
                continue
        out.append(line)
    out.extend(f"{key}={value}" for key, value in remaining.items())

    mode = (path.stat().st_mode & 0o777) if path.exists() else 0o600
    handle_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".env.")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(out).rstrip("\n") + "\n")
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def render_qr_svg(payload: str) -> str:
    """The same code as an SVG document.

    The terminal drawing is the default because it needs no files, but it is
    the fragile one: a proportional font, a small window, or a QR that has
    scrolled half out of view all produce something a camera will not read.
    An SVG is drawn at whatever size the viewer gives it, and `qrcode` ships
    the factory -- no Pillow, no image library.
    """
    code = qrcode.QRCode(border=2)
    code.add_data(payload)
    code.make(fit=True)
    image = code.make_image(image_factory=SvgPathImage)
    buffer = io.BytesIO()
    image.save(buffer)
    return buffer.getvalue().decode("utf-8")


async def run_login(
    env_path: Path,
    *,
    timeout: float = 480.0,
    image_path: Path | None = None,
    opener: Callable[[Path], None] | None = None,
    show: Callable[[str], None] = print,
) -> LoginResult:
    """Pair, then persist the result.

    The QR is drawn before the wait starts and the URL is printed as well, so
    a terminal too narrow for the block characters -- or an operator on another
    machine -- still has a way through. When `image_path` is given the code is
    also written there, and `opener` is called on it, because "scan this" is
    the one step a person has to do and it should not depend on their font.

    `wait_for_login` gives up after `timeout` rather than polling forever: an
    expired code is a normal outcome, not a hang to be babysat.
    """
    qr: LoginQrcode = await request_login_qrcode()

    if image_path is not None:
        image_path.parent.mkdir(parents=True, exist_ok=True)
        image_path.write_text(render_qr_svg(qr.qrcode_img_content), encoding="utf-8")
    else:
        show(render_qr(qr.qrcode_img_content))

    show("Scan this with WeChat:" if image_path else "Scan the code above with WeChat:")
    show(f"  {qr.qrcode_img_content}")
    if image_path is not None:
        show(f"  image: {image_path}")
        if opener is not None:
            opener(image_path)
    show("")

    result = await wait_for_login(qr.qrcode, timeout=timeout)
    upsert_env(
        env_path,
        {BOT_TOKEN_KEY: result.bot_token, BASE_URL_KEY: result.base_url},
    )
    log.info("wechat_paired", bot_id=result.bot_id, base_url=result.base_url)
    return result
