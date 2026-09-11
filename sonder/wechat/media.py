"""Inbound media from the WeChat CDN.

Everything WeChat stores on its CDN is encrypted, and the key travels in the
message rather than being derived. Three details come straight from the
official client because they are not guessable from the wire format:

* **Two key encodings.** `aes_key` is base64, but images carry base64 of the
  raw 16 bytes while file, voice and video carry base64 of a 32-character hex
  string. Decoding the second kind without the extra hex step yields 32 bytes
  and a cipher that fails on padding.
* **Two URL shapes.** A message may carry `full_url` outright; otherwise the
  URL is built from `encrypt_query_param` against the CDN base.
* **Images name the key twice.** `image_item.aeskey` is tried first and
  `image_item.media.aes_key` second. A message with no key at all is
  downloaded as plain bytes rather than refused -- the official client does
  the same, and refusing would lose an image that is simply not encrypted.

The fields are read out of `WeixinMessage.raw`, which the client keeps for
exactly this: it models text and leaves everything else as it arrived.
"""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
import structlog
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .client import WeixinMessage

log = structlog.get_logger(__name__)

# The CDN base the official client falls back to when a message has no
# `full_url`. `novac2c` is Tencent's own image CDN.
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

DOWNLOAD_TIMEOUT_S = 30.0

# `item_list[].type`. Images only, for now: a forwarded screenshot is what
# this channel is for, and the other kinds need a decision about what sonder
# does with a voice note or a PDF before they are worth reading.
ITEM_IMAGE = 2

_IMAGE_FIELD = "image_item"

_HEX = re.compile(r"[0-9a-fA-F]+")


def _is_hex32(value: str) -> bool:
    return len(value) == 32 and bool(_HEX.fullmatch(value))


class MediaError(RuntimeError):
    """A media item could not be turned into bytes."""


def parse_aes_key(encoded: str) -> bytes:
    """The 16 raw bytes behind a `CDNMedia.aes_key` / `image_item.aeskey`.

    Three shapes turn up, and the official client handles two of them:

    * base64 of the raw 16 bytes -- what images usually carry
    * base64 of a 32-character hex string -- file, voice, video
    * **the 32-character hex string itself**, which is the one the official
      client throws on: read as base64 it decodes to 24 bytes, which is
      neither of its accepted lengths.

    Hex is checked *before* base64 because a 32-character hex string is also
    valid base64 -- the order is what decides it, and getting it wrong fails
    silently until an image will not decrypt. This was found that way: a real
    forwarded picture came back as `got 24`, which is the shape of exactly
    this case.

    Anything else is an error. Guessing past these three would produce bytes
    a caller could mistake for a corrupt image.
    """
    candidate = encoded.strip()
    if _is_hex32(candidate):
        return bytes.fromhex(candidate)

    try:
        decoded = base64.b64decode(candidate)
    except (binascii.Error, ValueError) as exc:
        raise MediaError(f"aes_key is not valid base64: {exc}") from exc

    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        try:
            return bytes.fromhex(decoded.decode("ascii"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise MediaError("aes_key decodes to 32 bytes but is not hex") from exc
    raise MediaError(
        "aes_key is not a 16-byte key, a base64 hex string, or a hex string "
        f"(decoded to {len(decoded)} bytes from {len(candidate)} characters)"
    )


def _unpad_pkcs7(data: bytes) -> bytes:
    """Strip PKCS7 padding, and insist it is really there.

    Node's `createDecipheriv` removes PKCS7 by itself, so the official client
    never writes this and a port that skips it looks correct. Python's
    `cryptography` does not, which means skipping it returns the plaintext
    followed by 1..16 bytes of padding -- and an image decoder handed that
    reports a truncated or corrupt file, not a bug in this channel.

    Checking rather than slicing is also the only cheap signal that the key was
    right: a wrong key essentially never ends in a valid pad-byte run.
    """
    if not data:
        raise MediaError("cannot unpad an empty buffer")
    pad = data[-1]
    if not 1 <= pad <= 16 or len(data) < pad:
        raise MediaError(f"invalid PKCS7 padding (pad byte {pad}, {len(data)} bytes)")
    if data[-pad:] != bytes([pad]) * pad:
        raise MediaError("invalid PKCS7 padding (pad bytes disagree)")
    return data[:-pad]


def decrypt_aes_ecb(ciphertext: bytes, key: bytes) -> bytes:
    """AES-128-ECB with PKCS7 padding, which is the CDN's format.

    ECB is normally the wrong mode and is worth naming as such -- it leaks
    structure across equal blocks. This is not a choice being made here: it is
    what every object on this CDN uses, and the alternative to decrypting it is
    not reading forwarded images at all.
    """
    if len(key) != 16:
        raise MediaError(f"aes-128 needs a 16-byte key, got {len(key)}")
    if len(ciphertext) % 16:
        raise MediaError(
            f"ciphertext is {len(ciphertext)} bytes, not a multiple of the block size"
        )
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    try:
        padded = decryptor.update(ciphertext) + decryptor.finalize()
    except ValueError as exc:
        raise MediaError(f"decryption failed: {exc}") from exc
    return _unpad_pkcs7(padded)


@dataclass(frozen=True, slots=True)
class ImageRef:
    """Where an inbound image is, and how to read it."""

    url: str
    aes_key: str | None = None


def _media_of(item: Mapping[str, Any]) -> Mapping[str, Any] | None:
    holder = item.get(_IMAGE_FIELD)
    if not isinstance(holder, Mapping):
        return None
    return holder


def image_reference(
    message: WeixinMessage, *, cdn_base: str = CDN_BASE_URL
) -> ImageRef | None:
    """The first image in a message, or None.

    Returns None rather than raising for anything that is not an image with a
    locatable body: a message without media is the common case, and a caller
    should not have to tell "no picture" apart from "a broken one".
    """
    for item in message.raw.get("item_list") or ():
        if not isinstance(item, Mapping) or item.get("type") != ITEM_IMAGE:
            continue
        holder = _media_of(item)
        if holder is None:
            continue
        media = holder.get("media")
        media = media if isinstance(media, Mapping) else {}

        full_url = media.get("full_url")
        query = media.get("encrypt_query_param")
        if isinstance(full_url, str) and full_url:
            # Preferred: the server already decided where the object lives.
            url = full_url
        elif isinstance(query, str) and query:
            # The official client's fallback. `quote(..., safe="")` is the
            # Python spelling of `encodeURIComponent`; encoding a character it
            # would have left alone is harmless, the reverse is not.
            url = f"{cdn_base}/download?encrypted_query_param={quote(query, safe='')}"
        else:
            continue

        # `image_item.aeskey` first, then `media.aes_key`. The official client
        # prefers the former and falls back to the latter.
        key = holder.get("aeskey") or media.get("aes_key")
        return ImageRef(url=url, aes_key=key if isinstance(key, str) and key else None)
    return None


def describe_image(message: WeixinMessage) -> str:
    """The shape of a message's image item, safe to log.

    Written after a real forwarded picture failed with `got 24` and there was
    nothing in the log to say what the key had looked like. The useful facts
    are which field it came from, how long it is, and whether it is hex --
    never the value, which is a working decryption key for the object it
    belongs to.
    """
    for item in message.raw.get("item_list") or ():
        if not isinstance(item, Mapping) or item.get("type") != ITEM_IMAGE:
            continue
        holder = _media_of(item) or {}
        media = holder.get("media")
        media = media if isinstance(media, Mapping) else {}
        key = holder.get("aeskey") or media.get("aes_key")
        source = "image_item.aeskey" if holder.get("aeskey") else "media.aes_key"
        return " ".join(
            [
                f"source={source}",
                f"key_chars={len(key) if isinstance(key, str) else 0}",
                f"key_is_hex32={isinstance(key, str) and _is_hex32(key)}",
                f"has_full_url={bool(media.get('full_url'))}",
                f"has_query={bool(media.get('encrypt_query_param'))}",
                f"media_fields={sorted(media)}",
                f"item_fields={sorted(item)}",
            ]
        )
    return "no image item"


async def download_image(
    ref: ImageRef, *, http: httpx.AsyncClient | None = None
) -> bytes:
    """Fetch an image and decrypt it if it carries a key.

    The client is borrowed when one is passed in and made otherwise, so a
    caller with a pool shares it and a one-shot call does not leak one.
    """
    if http is not None:
        return await _fetch(ref, http)

    async with httpx.AsyncClient(
        timeout=DOWNLOAD_TIMEOUT_S, follow_redirects=False
    ) as made:
        return await _fetch(ref, made)


async def _fetch(ref: ImageRef, http: httpx.AsyncClient) -> bytes:
    try:
        response = await http.get(ref.url, timeout=DOWNLOAD_TIMEOUT_S)
    except httpx.HTTPError as exc:
        raise MediaError(f"CDN download failed: {exc!r}") from exc

    if response.status_code >= 400:
        raise MediaError(
            f"CDN download returned {response.status_code} for {ref.url.split('?')[0]}"
        )

    body = response.content
    if ref.aes_key is None:
        # No key means the object was stored unencrypted. The official client
        # reads it as-is rather than refusing, and so does this.
        log.debug("wechat_media_plain", bytes=len(body))
        return body

    plain = decrypt_aes_ecb(body, parse_aes_key(ref.aes_key))
    log.debug("wechat_media_decrypted", encrypted=len(body), plain=len(plain))
    return plain
