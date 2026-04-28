"""Integration tests for the /donations FastAPI router.

We stub out the JTXDropService and XahauBadgeService so tests don't
hit real RPC. Idempotency, HMAC verification, and event parsing are
all exercised against the real implementations.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import donations as donations_module
from routers.donations import router as donations_router, configure_services
from services.idempotency import IdempotencyStore


HELIUS_SECRET = "router-test-secret"


class StubJTX:
    """Stand-in for JTXDropService that records calls instead of hitting RPC."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def drop(self, donor_wallet: str) -> str:
        self.calls.append(donor_wallet)
        return "STUB_JTX_TX_" + donor_wallet[:8]


class StubXahau:
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.calls: list[tuple[str, str, str]] = []

    async def trigger_badge(self, donor_xahau_addr: str, donation_id: str, sol_tx_sig: str) -> str:
        self.calls.append((donor_xahau_addr, donation_id, sol_tx_sig))
        return "STUB_XAHAU_TX"


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.setenv("HELIUS_WEBHOOK_SECRET", HELIUS_SECRET)
    monkeypatch.setenv("JTX_DAILY_DROP_CAP", "1000")
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")

    store = IdempotencyStore(db_path=":memory:")
    jtx = StubJTX()
    xahau = StubXahau()
    configure_services(store=store, jtx=jtx, xahau=xahau)

    fastapi_app = FastAPI()
    fastapi_app.include_router(donations_router)
    # stash for tests
    fastapi_app.state.store = store
    fastapi_app.state.jtx = jtx
    fastapi_app.state.xahau = xahau
    return fastapi_app


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


VALID_SIG = "5j7s" + "1" * 70  # base58-ish length 74
VALID_WALLET = "EFvgELE1Hb4PC5tbPTAe8v1uEDGee8nwYBMCU42bZRGk"


def _hmac_sig(body: bytes) -> str:
    return hmac.new(HELIUS_SECRET.encode(), body, hashlib.sha256).hexdigest()


# ── /donations/claim ──────────────────────────────────────────────────────


def test_claim_accepts_new_donation(client: TestClient) -> None:
    r = client.post(
        "/donations/claim",
        json={
            "sol_tx_sig": VALID_SIG,
            "donor_wallet": VALID_WALLET,
            "lamports": 500_000_000,
            "xahau_address": "rDNvpqSzRzgpBZi6cZGSXyf3bVDB8VqFqV",
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["accepted"] is True
    assert data["is_new"] is True
    assert data["record"]["rewards"]["xahau_badge"]["status"] == "pending"


def test_claim_rejects_invalid_sig(client: TestClient) -> None:
    r = client.post(
        "/donations/claim",
        json={
            "sol_tx_sig": "too-short",
            "donor_wallet": VALID_WALLET,
            "lamports": 500_000_000,
        },
    )
    assert r.status_code == 400


def test_claim_rejects_invalid_xahau(client: TestClient) -> None:
    r = client.post(
        "/donations/claim",
        json={
            "sol_tx_sig": VALID_SIG,
            "donor_wallet": VALID_WALLET,
            "lamports": 500_000_000,
            "xahau_address": "NotARippleAddress!@#",
        },
    )
    assert r.status_code == 400


def test_duplicate_claim_returns_existing(client: TestClient) -> None:
    payload = {
        "sol_tx_sig": VALID_SIG,
        "donor_wallet": VALID_WALLET,
        "lamports": 500_000_000,
    }
    client.post("/donations/claim", json=payload)
    r = client.post("/donations/claim", json=payload)
    assert r.status_code == 200
    assert r.json()["is_new"] is False


# ── /donations/webhooks/helius ────────────────────────────────────────────


def test_webhook_rejects_missing_signature(client: TestClient) -> None:
    body = b'[]'
    r = client.post(
        "/donations/webhooks/helius",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 401


def test_webhook_rejects_bad_signature(client: TestClient) -> None:
    body = b'[]'
    r = client.post(
        "/donations/webhooks/helius",
        content=body,
        headers={
            "content-type": "application/json",
            "x-helius-signature": "deadbeef" * 8,
        },
    )
    assert r.status_code == 401


def test_webhook_processes_native_transfer(client: TestClient, app: FastAPI) -> None:
    event = {
        "signature": VALID_SIG,
        "feePayer": VALID_WALLET,
        "nativeTransfers": [
            {
                "fromUserAccount": VALID_WALLET,
                "toUserAccount": "CQmBrff4a9MouY4eQTAPKXKprtvfHZDNSkNCQhRR38tP",
                "amount": 500_000_000,
            }
        ],
    }
    body = json.dumps([event]).encode()
    r = client.post(
        "/donations/webhooks/helius",
        content=body,
        headers={
            "content-type": "application/json",
            "x-helius-signature": _hmac_sig(body),
        },
    )
    assert r.status_code == 200, r.text
    assert r.json() == {"accepted": 1, "total": 1}

    # Background task should have invoked our stub JTX service.
    jtx: StubJTX = app.state.jtx
    # TestClient's BackgroundTasks run synchronously after the response.
    assert jtx.calls == [VALID_WALLET]


def test_webhook_skips_failed_tx(client: TestClient, app: FastAPI) -> None:
    body = json.dumps([{
        "signature": VALID_SIG,
        "transactionError": {"InstructionError": [0, "Custom"]},
        "feePayer": VALID_WALLET,
        "nativeTransfers": [{"fromUserAccount": VALID_WALLET, "amount": 1}],
    }]).encode()
    r = client.post(
        "/donations/webhooks/helius",
        content=body,
        headers={
            "content-type": "application/json",
            "x-helius-signature": _hmac_sig(body),
        },
    )
    assert r.status_code == 200
    jtx: StubJTX = app.state.jtx
    assert jtx.calls == []


def test_webhook_picks_up_xahau_from_prior_claim(client: TestClient, app: FastAPI) -> None:
    # First the dapp tells us about the xahau address...
    client.post(
        "/donations/claim",
        json={
            "sol_tx_sig": VALID_SIG,
            "donor_wallet": VALID_WALLET,
            "lamports": 500_000_000,
            "xahau_address": "rDNvpqSzRzgpBZi6cZGSXyf3bVDB8VqFqV",
        },
    )
    # Then Helius confirms.
    event = {
        "signature": VALID_SIG,
        "feePayer": VALID_WALLET,
        "nativeTransfers": [{"fromUserAccount": VALID_WALLET, "amount": 500_000_000}],
    }
    body = json.dumps([event]).encode()
    r = client.post(
        "/donations/webhooks/helius",
        content=body,
        headers={
            "content-type": "application/json",
            "x-helius-signature": _hmac_sig(body),
        },
    )
    assert r.status_code == 200

    xahau: StubXahau = app.state.xahau
    assert len(xahau.calls) == 1
    assert xahau.calls[0][0] == "rDNvpqSzRzgpBZi6cZGSXyf3bVDB8VqFqV"


# ── /donations/status ─────────────────────────────────────────────────────


def test_status_endpoint(client: TestClient) -> None:
    client.post(
        "/donations/claim",
        json={
            "sol_tx_sig": VALID_SIG,
            "donor_wallet": VALID_WALLET,
            "lamports": 500_000_000,
        },
    )
    r = client.get(f"/donations/status/{VALID_SIG}")
    assert r.status_code == 200
    assert r.json()["sol_tx_sig"] == VALID_SIG


def test_status_404_for_unknown(client: TestClient) -> None:
    r = client.get(f"/donations/status/{'9' * 64}")
    assert r.status_code == 404


# ── /donations/admin/replay ───────────────────────────────────────────────


def test_admin_replay_requires_token(client: TestClient) -> None:
    client.post(
        "/donations/claim",
        json={"sol_tx_sig": VALID_SIG, "donor_wallet": VALID_WALLET, "lamports": 100_000_000},
    )
    r = client.post(f"/donations/admin/replay/{VALID_SIG}")
    assert r.status_code == 401


def test_admin_replay_accepts_with_token(client: TestClient) -> None:
    client.post(
        "/donations/claim",
        json={"sol_tx_sig": VALID_SIG, "donor_wallet": VALID_WALLET, "lamports": 100_000_000},
    )
    r = client.post(
        f"/donations/admin/replay/{VALID_SIG}",
        headers={"x-admin-token": "test-admin-token"},
    )
    assert r.status_code == 200
    assert "jtx_drop" in r.json()["replayed"]
