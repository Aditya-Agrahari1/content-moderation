"""HMAC-SHA256 over the exact raw request body, hex encoded, optionally prefixed with "sha256="."""

import hashlib
import hmac


def sign(secret: str, raw_body: bytes) -> str:
    return hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()


def verify(secret: str, raw_body: bytes, header_value: str | None) -> bool:
    if not header_value:
        return False
    candidate = header_value.strip().removeprefix("sha256=")
    return hmac.compare_digest(sign(secret, raw_body), candidate.lower())
