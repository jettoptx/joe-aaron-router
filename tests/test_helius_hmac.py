"""Unit tests for Helius webhook auth-header verification.

Helius forwards the value of the dashboard's "Authentication Header"
field as the `Authorization` header on every POST — shared bearer
token, not HMAC. We accept any of:
  - the bare secret
  - `Bearer <secret>`
  - `sha256=<secret>` (legacy HMAC-style wrapper, still accepted)
And reject anything else.
"""

from __future__ import annotations

import pytest

from security.helius_hmac import HeliusSignatureError, verify_helius_signature


SECRET = "test-secret-do-not-use-in-prod"
BODY = b'[{"signature":"abc","fee":5000}]'


def test_valid_secret_accepts() -> None:
    headers = {"authorization": SECRET}
    verify_helius_signature(BODY, headers, secret=SECRET)  # no raise


def test_x_helius_signature_header_also_accepted() -> None:
    headers = {"x-helius-signature": SECRET}
    verify_helius_signature(BODY, headers, secret=SECRET)


def test_bearer_prefix_stripped() -> None:
    headers = {"authorization": f"Bearer {SECRET}"}
    verify_helius_signature(BODY, headers, secret=SECRET)


def test_sha256_prefix_stripped() -> None:
    headers = {"authorization": f"sha256={SECRET}"}
    verify_helius_signature(BODY, headers, secret=SECRET)


def test_missing_header_raises() -> None:
    with pytest.raises(HeliusSignatureError, match="missing"):
        verify_helius_signature(BODY, {}, secret=SECRET)


def test_wrong_secret_raises() -> None:
    headers = {"authorization": "deadbeef" * 8}
    with pytest.raises(HeliusSignatureError, match="mismatch"):
        verify_helius_signature(BODY, headers, secret=SECRET)


def test_missing_secret_env_raises() -> None:
    with pytest.raises(HeliusSignatureError, match="not configured"):
        verify_helius_signature(BODY, {}, secret="")


def test_case_insensitive_header_lookup() -> None:
    headers = {"Authorization": SECRET}
    verify_helius_signature(BODY, headers, secret=SECRET)


def test_body_is_ignored_for_auth() -> None:
    # The body could be anything — auth is purely header-based now.
    headers = {"authorization": SECRET}
    verify_helius_signature(b"completely different body", headers, secret=SECRET)
