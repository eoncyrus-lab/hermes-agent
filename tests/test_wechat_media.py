"""Inbound WeChat media, without a WeChat account behind it.

The CDN half of this channel cannot be exercised for real until somebody pairs
a bot and forwards a picture, but almost none of it needs to be: the key
encoding, the AES mode, the URL the client builds and the decrypt itself are
all pure, and a mocked CDN response covers the rest. What is *not* covered here
is the real CDN's behaviour -- if Tencent changes the object format, these
tests will keep passing and the first forwarded screenshot will fail.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from sonder.wechat.client import WeixinMessage
from sonder.wechat.media import (
    CDN_BASE_URL,
    MediaError,
    describe_image,
    download_image,
    image_reference,
    parse_aes_key,
)

KEY = bytes(range(16))


def _encrypt(plaintext: bytes, key: bytes = KEY) -> bytes:
    """AES-128-ECB with PKCS7, spelled out.

    Node's `createCipheriv` pads by default and Python's `cryptography` does
    not, so the padding has to be written here -- and its absence in the
    implementation is exactly the bug the round-trip test exists to catch.
    """
    pad = 16 - len(plaintext) % 16
    padded = plaintext + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _message(**image_item: Any) -> WeixinMessage:
    """A message shaped the way the gateway sends one, raw and unmodelled."""
    return WeixinMessage(
        from_user_id="o9cq@im.wechat",
        raw={
            "item_list": [
                {"type": 2, "image_item": image_item},
            ]
        },
    )


def _media(**fields: Any) -> dict[str, Any]:
    return {"media": fields}


# ------------------------------------------------------------- the aes key


def test_a_key_of_sixteen_raw_bytes_is_used_as_is():
    assert parse_aes_key(base64.b64encode(KEY).decode()) == KEY


def test_a_key_of_thirty_two_hex_characters_is_parsed_as_hex():
    """File, voice and video base64 a hex string rather than the bytes. Taking
    it at face value yields a 32-byte key and a cipher that fails on padding,
    which reads as a corrupt image rather than as a decoding mistake."""
    encoded = base64.b64encode(KEY.hex().encode("ascii")).decode()
    assert parse_aes_key(encoded) == KEY


@pytest.mark.parametrize("bad", ["", base64.b64encode(b"x" * 20).decode()])
def test_a_key_of_the_wrong_length_is_refused(bad):
    with pytest.raises(MediaError, match="not a 16-byte key"):
        parse_aes_key(bad)


def test_a_key_that_is_not_base64_is_refused_before_its_length_is_considered():
    """`short` is ten bits of base64, not a short key. Reporting it as the
    wrong length would send a reader looking at the key size."""
    with pytest.raises(MediaError, match="not valid base64"):
        parse_aes_key("short")


def test_a_thirty_two_byte_key_that_is_not_hex_is_refused():
    encoded = base64.b64encode(b"z" * 32).decode()
    with pytest.raises(MediaError, match="not hex"):
        parse_aes_key(encoded)


# --------------------------------------------------------------- decrypting


def test_pkcs7_round_trip():
    plaintext = "这是一段中文，长度不是 16 的倍数".encode()
    assert len(plaintext) % 16 != 0, "the padding has to actually do work"

    from sonder.wechat.media import decrypt_aes_ecb

    assert decrypt_aes_ecb(_encrypt(plaintext), KEY) == plaintext


def test_a_wrong_key_is_an_error_not_garbage():
    from sonder.wechat.media import decrypt_aes_ecb

    with pytest.raises(MediaError, match="padding|decryption failed"):
        decrypt_aes_ecb(_encrypt(b"hello"), bytes(range(1, 17)))


def test_a_key_of_the_wrong_size_is_refused():
    from sonder.wechat.media import decrypt_aes_ecb

    with pytest.raises(MediaError, match="16-byte key"):
        decrypt_aes_ecb(b"\x00" * 16, b"short")


# -------------------------------------------------------------- the message


def test_full_url_wins_over_the_fallback():
    ref = image_reference(
        _message(**_media(full_url="https://cdn.example/x", aes_key="k"))
    )
    assert ref is not None and ref.url == "https://cdn.example/x"


def test_the_fallback_url_is_built_and_encoded():
    """The query parameter is opaque and can contain characters that would
    otherwise end the URL early."""
    ref = image_reference(_message(**_media(encrypt_query_param="a/b+c=d")))
    assert ref is not None
    assert ref.url.startswith(f"{CDN_BASE_URL}/download?encrypted_query_param=")
    assert "a%2Fb%2Bc%3Dd" in ref.url


def test_the_image_level_key_is_preferred():
    ref = image_reference(
        _message(
            aeskey="image-level", **_media(encrypt_query_param="q", aes_key="media-level")
        )
    )
    assert ref is not None and ref.aes_key == "image-level"


def test_the_media_level_key_is_the_fallback():
    ref = image_reference(
        _message(**_media(encrypt_query_param="q", aes_key="media-level"))
    )
    assert ref is not None and ref.aes_key == "media-level"


def test_a_message_with_no_image_is_not_an_error():
    text = WeixinMessage(raw={"item_list": [{"type": 1, "text_item": {"text": "hi"}}]})
    assert image_reference(text) is None


def test_an_image_with_no_located_body_is_skipped():
    assert image_reference(_message(**_media())) is None


# -------------------------------------------------------------- the fetching


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_an_encrypted_image_is_downloaded_and_decrypted():
    plaintext = b"\xff\xd8\xff\xe0 pretend jpeg bytes" * 3
    ref = image_reference(
        _message(
            **_media(
                full_url="https://cdn.example/img",
                aes_key=base64.b64encode(KEY).decode(),
            )
        )
    )
    assert ref is not None

    async with _client(
        lambda request: httpx.Response(200, content=_encrypt(plaintext))
    ) as http:
        assert await download_image(ref, http=http) == plaintext


async def test_an_image_with_no_key_is_taken_as_plain_bytes():
    """The official client reads a keyless object as-is instead of refusing.
    Refusing would lose an image that simply is not encrypted."""
    ref = image_reference(_message(**_media(full_url="https://cdn.example/plain")))
    assert ref is not None

    async with _client(lambda request: httpx.Response(200, content=b"raw jpeg")) as http:
        assert await download_image(ref, http=http) == b"raw jpeg"


async def test_a_cdn_error_is_reported_without_the_query_string():
    ref = image_reference(_message(**_media(encrypt_query_param="secret-ish-token")))
    assert ref is not None

    async with _client(lambda request: httpx.Response(404, content=b"nope")) as http:
        with pytest.raises(MediaError) as excinfo:
            await download_image(ref, http=http)

    assert "404" in str(excinfo.value)
    assert "secret-ish-token" not in str(excinfo.value), "the token is a capability"


async def test_a_transport_failure_is_wrapped():
    def boom(request):
        raise httpx.ConnectError("no route to host")

    ref = image_reference(_message(**_media(full_url="https://cdn.example/x")))
    assert ref is not None

    async with _client(boom) as http:
        with pytest.raises(MediaError, match="CDN download failed"):
            await download_image(ref, http=http)


def test_the_reference_survives_a_round_trip_through_json():
    """`raw` is a decoded JSON object, so nothing in it is a Python-only type.
    Asserted because the parser reads it positionally and a shape change would
    otherwise show up as a silently missing image."""
    payload = {
        "item_list": [
            {
                "type": 2,
                "image_item": {"media": {"full_url": "https://x", "aes_key": "k"}},
            }
        ]
    }
    message = WeixinMessage(raw=json.loads(json.dumps(payload)))
    ref = image_reference(message)
    assert ref is not None and ref.url == "https://x"


def test_a_raw_hex_key_is_accepted():
    """Seen in the wild, and the shape the official client throws on: read as
    base64 a 32-character hex string decodes to 24 bytes, which is neither of
    its accepted lengths. A real forwarded picture came back as `got 24`."""
    assert parse_aes_key(KEY.hex()) == KEY


def test_hex_is_checked_before_base64():
    """A 32-character hex string is also valid base64, so the order is what
    decides it -- and getting it wrong fails only when an image will not
    decrypt."""
    hexish = "ab" * 16
    assert parse_aes_key(hexish) == bytes.fromhex(hexish)
    assert parse_aes_key(hexish) != base64.b64decode(hexish)[:16]


def test_a_key_of_a_fourth_shape_names_what_it_saw():
    """The error has to carry enough to diagnose it: the first version said
    only `got 24`, with nothing about where the value came from."""
    with pytest.raises(MediaError) as excinfo:
        parse_aes_key("x" * 40)

    message = str(excinfo.value)
    assert "16-byte key" in message and "hex string" in message
    assert "40 characters" in message


def test_the_diagnostic_describes_the_key_without_printing_it():
    secret = KEY.hex()
    message = WeixinMessage(
        raw={
            "item_list": [
                {
                    "type": 2,
                    "image_item": {"aeskey": secret, "media": {"full_url": "https://x"}},
                }
            ]
        }
    )
    described = describe_image(message)

    assert secret not in described, "a working decryption key is not log output"
    assert "key_chars=32" in described and "key_is_hex32=True" in described
    assert "source=image_item.aeskey" in described
