"""
Helius webhook HMAC verification.

Helius signs webhook payloads with HMAC-SHA256 using the secret you set
on the webhook in dashboard.helius.dev. The signature arrives in the
`Authorization` or `X-Helius-Signature` header (Helius has used both
during 2025-2026 — we accept either).

We compare in constant time to prevent timing-oracle attacks.
"""

from __future__ import annotations

import hashlib
import hmac
import os


HELIUS_HEADER_CANDIDATES = (
    "x-helius-signature",
    "authorization",
)


class HeliusSignatureError(Exception):
    """Raised when a Helius webhook signature is missing or invalid."""


def _get_signature(headers: dict[str, str]) -> str | None:
    # Headers are case-insensitive — normalize to lowercase keys.
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in HELIUS_HEADER_CANDIDATES:
        if name in lowered:
            value = lowered[name]
            # Some webhook providers prefix with `sha256=`. Strip if present.
            if value.lower().startswith("sha256="):
                value = value.split("=", 1)[1]
            return value
    return None


def verify_helius_signature(
    raw_body: bytes,
    headers: dict[str, str],
    secret: str | None = None,
) -> None:
    """Verify a Helius webhook payload. Raises HeliusSignatureError on failure.

    Args:
      raw_body: the exact bytes of the request body as received.
      headers: request headers (any case).
      secret:  HMAC secret. If None, reads from env `HELIUS_WEBHOOK_SECRET`.
               If still empty, raises (we never accept unsigned payloads).
    """
    secret = secret if secret is not None else os.getenv("HELIUS_WEBHOOK_SECRET", "")
    if not secret:
        raise HeliusSignatureError("HELIUS_WEBHOOK_SECRET is not configured")

    provided = _get_signature(headers)
    if not provided:
        raise HeliusSignatureError("missing Helius signature header")

    expected = hmac.new(
        secret.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(expected, provided):
        raise HeliusSignatureError("Helius signature mismatch")
