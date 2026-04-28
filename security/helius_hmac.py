"""
Helius webhook auth-header verification.

Helius's webhook config (dashboard.helius.dev → Webhooks → Add Webhook)
exposes an "Authentication Header" field. Helius forwards that exact
string back to us as the `Authorization` header on every webhook POST —
it's a shared bearer token, NOT an HMAC of the body.

We compare in constant time to prevent timing-oracle attacks.

Function name kept (`verify_helius_signature`) for API stability across
the existing routers/tests; the implementation is shared-secret compare.
The `raw_body` parameter is unused but kept so callers don't need to
change.
"""

from __future__ import annotations

import hmac
import os


HELIUS_HEADER_CANDIDATES = (
    "authorization",
    "x-helius-signature",  # legacy — still accepted in case Helius adds HMAC
)


class HeliusSignatureError(Exception):
    """Raised when a Helius webhook auth header is missing or invalid."""


def _get_signature(headers: dict[str, str]) -> str | None:
    # Headers are case-insensitive — normalize to lowercase keys.
    lowered = {k.lower(): v for k, v in headers.items()}
    for name in HELIUS_HEADER_CANDIDATES:
        if name in lowered:
            value = lowered[name]
            # Tolerate wrappers some users add: `Bearer <secret>` or
            # `sha256=<value>`. Strip and compare the raw secret.
            if value.lower().startswith("bearer "):
                value = value.split(" ", 1)[1]
            elif value.lower().startswith("sha256="):
                value = value.split("=", 1)[1]
            return value
    return None


def verify_helius_signature(
    raw_body: bytes,  # noqa: ARG001 — kept for API stability
    headers: dict[str, str],
    secret: str | None = None,
) -> None:
    """Verify a Helius webhook auth header. Raises HeliusSignatureError on failure.

    Args:
      raw_body: unused (kept for API stability with HMAC-style callers).
      headers:  request headers (any case).
      secret:   shared secret that you pasted into the Helius webhook's
                "Authentication Header" field. Reads `HELIUS_WEBHOOK_SECRET`
                env var if None. Raises if still empty (we never accept
                unauthenticated payloads).
    """
    secret = secret if secret is not None else os.getenv("HELIUS_WEBHOOK_SECRET", "")
    if not secret:
        raise HeliusSignatureError("HELIUS_WEBHOOK_SECRET is not configured")

    provided = _get_signature(headers)
    if not provided:
        raise HeliusSignatureError("missing Helius auth header")

    if not hmac.compare_digest(secret, provided):
        raise HeliusSignatureError("Helius auth header mismatch")
