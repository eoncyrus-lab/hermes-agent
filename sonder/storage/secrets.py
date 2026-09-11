"""Encrypting what the database should not hold in the clear.

`vault_records` has carried a `ciphertext` column and a `key_version` since
the first migration and nothing ever wrote to it, because nothing needed to
until now. A model credential is different from the rows around it: it spends
somebody's money, it is portable, and a database dump that contains it is a
database dump that contains working keys.

The key lives in the environment (`SONDER_VAULT_KEY`) rather than in the
database, so the two have to be taken together to be worth anything. That is
the whole design, and it is why the key must not be stored beside the dump.

Rotation is `key_version` plus more than one key: `SONDER_VAULT_KEY` accepts a
comma-separated list, the first entry encrypts, and every entry is tried when
decrypting, so a new key can be prepended and old rows keep working.
"""

from __future__ import annotations

import hashlib

import structlog
from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

log = structlog.get_logger(__name__)


class MissingVaultKey(RuntimeError):
    """No key configured, so nothing can be encrypted or decrypted.

    Raised rather than falling back to plaintext: a credential written in the
    clear because a key was absent is worse than a request that fails and says
    which variable to set.
    """


class Undecryptable(RuntimeError):
    """The ciphertext does not match any configured key."""


def _keys() -> list[bytes]:
    raw = (settings().vault_key or "").strip()
    if not raw:
        raise MissingVaultKey(
            "SONDER_VAULT_KEY is not set; generate one with "
            '`python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"`'
        )
    return [part.strip().encode() for part in raw.split(",") if part.strip()]


def configured() -> bool:
    """Whether a key is present, without raising."""
    return bool((settings().vault_key or "").strip())


def _fingerprint(key: bytes) -> str:
    """A short, stable name for one key.

    `key_version` is meant to say which key a row was written with, so that a
    rotation can tell what is still on the old one. An index would not: after
    a rotation the old rows still say "v1" while the new ones do too. A
    fingerprint of the key itself does say.
    """
    return "v1:" + hashlib.sha256(key).hexdigest()[:12]


def encrypt(plaintext: str) -> tuple[bytes, str]:
    """Returns (ciphertext, key_version)."""
    key = _keys()[0]
    return Fernet(key).encrypt(plaintext.encode("utf-8")), _fingerprint(key)


def decrypt(ciphertext: bytes, key_version: str | None = None) -> str:
    """Try every configured key.

    `key_version` is accepted and logged rather than used to select: the
    fingerprint says which key wrote a row, but a row written before a
    rotation has no way to know the list changed, so trying them all is what
    actually makes rotation work.
    """
    for key in _keys():
        try:
            return Fernet(key).decrypt(ciphertext).decode("utf-8")
        except InvalidToken:
            continue
    log.warning("vault_undecryptable", key_version=key_version)
    raise Undecryptable(
        "no configured SONDER_VAULT_KEY can decrypt this row; "
        "the key that wrote it is no longer in the list"
    )
