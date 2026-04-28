"""Unit tests for Helius webhook HMAC verification."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from security.helius_hmac import HeliusSignatureError, verify_helius_signature


SECRET = "test-secret-do-not-use-in-prod"
BODY = b'[{"signature":"abc","fee":5000}]'


def _sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_accepts() -> None:
    headers = {"x-helius-signature": _sig(SECRET, BODY)}
    verify_helius_signature(BODY, headers, secret=SECRET)  # no raise


def test_authorization_header_also_accepted() -> None:
    headers = {"authorization": _sig(SECRET, BODY)}
    verify_helius_signature(BODY, headers, secret=SECRET)


def test_sha256_prefix_stripped() -> None:
    headers = {"x-helius-signature": "sha256=" + _sig(SECRET, BODY)}
    verify_helius_signature(BODY, headers, secret=SECRET)


def test_missing_header_raises() -> None:
    with pytest.raises(HeliusSignatureError, match="missing"):
        verify_helius_signature(BODY, {}, secret=SECRET)


def test_wrong_signature_raises() -> None:
    headers = {"x-helius-signature": "deadbeef" * 8}
    with pytest.raises(HeliusSignatureError, match="mismatch"):
        verify_helius_signature(BODY, headers, secret=SECRET)


def test_missing_secret_raises() -> None:
    with pytest.raises(HeliusSignatureError, match="not configured"):
        verify_helius_signature(BODY, {}, secret="")


def test_case_insensitive_header_lookup() -> None:
    headers = {"X-Helius-Signature": _sig(SECRET, BODY)}
    verify_helius_signature(BODY, headers, secret=SECRET)
