"""Unit tests for the donation idempotency store."""

from __future__ import annotations

import pytest

from services.idempotency import IdempotencyStore


@pytest.fixture
def store() -> IdempotencyStore:
    return IdempotencyStore(db_path=":memory:")


def test_first_claim_is_new(store: IdempotencyStore) -> None:
    rec, is_new = store.claim_or_fetch(
        sol_tx_sig="A" * 64,
        donor_wallet="D" * 32,
        lamports=500_000_000,
        xahau_address=None,
    )
    assert is_new is True
    assert rec.sol_tx_sig == "A" * 64
    assert rec.lamports == 500_000_000
    assert rec.jtx_drop_status == "pending"
    assert rec.xahau_status == "skipped"  # no xahau address


def test_second_claim_returns_existing(store: IdempotencyStore) -> None:
    sig = "B" * 64
    store.claim_or_fetch(
        sol_tx_sig=sig,
        donor_wallet="D1" * 16,
        lamports=1_000_000_000,
        xahau_address="rExampleXahauAddr",
    )
    rec, is_new = store.claim_or_fetch(
        sol_tx_sig=sig,
        donor_wallet="D2" * 16,  # would-be different wallet shouldn't overwrite
        lamports=999,
        xahau_address=None,
    )
    assert is_new is False
    assert rec.donor_wallet == "D1" * 16
    assert rec.lamports == 1_000_000_000
    assert rec.xahau_address == "rExampleXahauAddr"
    assert rec.xahau_status == "pending"


def test_jtx_drop_lifecycle(store: IdempotencyStore) -> None:
    sig = "C" * 64
    store.claim_or_fetch(sig, "wallet" * 6, 1_000_000_000, None)

    store.update_jtx_drop(sig, "in_flight", attempts_increment=1)
    rec = store.get(sig)
    assert rec is not None
    assert rec.jtx_drop_status == "in_flight"
    assert rec.jtx_drop_attempts == 1

    store.update_jtx_drop(sig, "done", tx_sig="JTXDROP" + "x" * 80)
    rec = store.get(sig)
    assert rec is not None
    assert rec.jtx_drop_status == "done"
    assert rec.jtx_drop_tx_sig and rec.jtx_drop_tx_sig.startswith("JTXDROP")


def test_public_dict_shape(store: IdempotencyStore) -> None:
    rec, _ = store.claim_or_fetch("E" * 64, "w" * 32, 100_000_000, "rXahau")
    pub = rec.to_public_dict()
    assert set(pub.keys()) == {"sol_tx_sig", "donor_wallet", "lamports", "rewards"}
    assert set(pub["rewards"].keys()) == {"jtx_drop", "xahau_badge", "metaplex_receipt"}
    # No internal error stacks leak to the public shape.
    flat = repr(pub)
    assert "error" not in flat.lower()
    assert "attempts" not in flat.lower()


def test_daily_cap_counts_only_recent(store: IdempotencyStore) -> None:
    # Inserting 3 in_flight rows.
    for i in range(3):
        sig = chr(ord("A") + i) * 64
        store.claim_or_fetch(sig, "wallet" * 6, 100_000_000, None)
        store.update_jtx_drop(sig, "in_flight", attempts_increment=1)
    assert store.daily_jtx_drop_count() == 3
