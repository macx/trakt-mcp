"""The owner passphrase: a secret only the server owner knows.

Every sign-in has to present it *before* a Trakt device code is issued. This
binds the device flow to the person who started it: an attacker who tricks the
owner into entering an attacker's code at trakt.tv/activate still cannot get
past our sign-in page (device-code phishing, RFC 8628 section 5.4).

Stored as a salted scrypt hash in the database (``trakt-mcp set-passphrase``).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

MIN_LENGTH = 12
# scrypt cost: ~16 MiB of memory and tens of milliseconds per attempt. Attempts
# are additionally serialised and rate limited by the provider.
_N, _R, _P, _DKLEN = 2**14, 8, 1, 32
_PREFIX = "scrypt"


def hash_passphrase(passphrase: str) -> str:
    if len(passphrase) < MIN_LENGTH:
        raise ValueError(f"The passphrase must have at least {MIN_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = _derive(passphrase, salt, _N, _R, _P)
    return "$".join((_PREFIX, str(_N), str(_R), str(_P), _b64(salt), _b64(digest)))


def verify_passphrase(passphrase: str, stored: str) -> bool:
    try:
        prefix, n, r, p, salt, digest = stored.split("$")
        if prefix != _PREFIX:
            return False
        expected = base64.b64decode(digest)
        actual = _derive(passphrase, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def _derive(passphrase: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=_DKLEN, maxmem=64 * 2**20
    )


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")
