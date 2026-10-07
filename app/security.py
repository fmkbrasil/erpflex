from __future__ import annotations

import base64
import hashlib
import hmac
import os

_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    if not password:
        raise ValueError("senha vazia")
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _ITERATIONS,
        base64.urlsafe_b64encode(salt).decode("ascii").rstrip("="),
        base64.urlsafe_b64encode(digest).decode("ascii").rstrip("="),
    )


def verify_password(password: str, encoded: str | None) -> bool:
    if not encoded or not password:
        return False
    try:
        scheme, iterations, salt64, digest64 = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        pad_salt = salt64 + "=" * (-len(salt64) % 4)
        pad_digest = digest64 + "=" * (-len(digest64) % 4)
        salt = base64.urlsafe_b64decode(pad_salt.encode("ascii"))
        expected = base64.urlsafe_b64decode(pad_digest.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False
