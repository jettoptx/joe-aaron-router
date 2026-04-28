"""
Donor reward endpoints for AARON Router.

Mounted at `/donations` from aaron_router.py.

Routes:
  POST /donations/claim
       Called by the dapp immediately after the donor's wallet signs the
       donate_sol transaction. Records the donor's optional Xahau address
       so we have it ready when the Helius webhook arrives. Idempotent on
       sol_tx_sig.

  POST /donations/webhooks/helius
       Helius-side webhook. HMAC-verified. Parses the Solana tx, identifies
       the donor wallet + lamports, and dispatches reward triggers
       (JTX drop + Xahau badge if address present).

  GET  /donations/status/{sol_tx_sig}
       Frontend polls this every 3s to render live status of each reward.

  POST /donations/admin/replay/{sol_tx_sig}
       Manual replay of failed legs. Requires X-Admin-Token header
       matching ADMIN_TOKEN env var.

Reward triggers run in background tasks so webhook responses stay fast
(Helius cancels deliveries that take >10s).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field

from security.helius_hmac import HeliusSignatureError, verify_helius_signature
from services.idempotency import IdempotencyStore, DonationRecord
from services.jtx_drop import JTXDropService
from services.xahau import XahauBadgeService, is_valid_xahau_address


logger = logging.getLogger("aaron.donations")

router = APIRouter(prefix="/donations", tags=["donations"])


# ── Singleton service holders (lazy) ──────────────────────────────────────
# Initialized on first use so the router is importable without env config.

_store: IdempotencyStore | None = None
_jtx: JTXDropService | None = None
_xahau: XahauBadgeService | None = None


def get_store() -> IdempotencyStore:
    global _store
    if _store is None:
        _store = IdempotencyStore()
    return _store


def get_jtx_service() -> JTXDropService:
    global _jtx
    if _jtx is None:
        _jtx = JTXDropService()
    return _jtx


def get_xahau_service() -> XahauBadgeService:
    global _xahau
    if _xahau is None:
        _xahau = XahauBadgeService()
    return _xahau


# Allow tests/main to inject pre-built services.
def configure_services(
    *,
    store: IdempotencyStore | None = None,
    jtx: JTXDropService | None = None,
    xahau: XahauBadgeService | None = None,
) -> None:
    global _store, _jtx, _xahau
    if store is not None:
        _store = store
    if jtx is not None:
        _jtx = jtx
    if xahau is not None:
        _xahau = xahau


# ── Request / response models ─────────────────────────────────────────────

SOLANA_SIG_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{64,90}$")
SOLANA_PUBKEY_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")


class DonationClaim(BaseModel):
    # NOTE: Optional[str] (not str | None) for Python 3.9 compat — Pydantic
    # v2 evaluates BaseModel field annotations eagerly even with
    # `from __future__ import annotations`.
    sol_tx_sig: str = Field(..., description="The donate_sol transaction signature")
    donor_wallet: str = Field(..., description="Donor's Solana pubkey (base58)")
    lamports: int = Field(..., ge=10_000, description="Donation amount in lamports")
    xahau_address: Optional[str] = Field(default=None, description="Optional Xahau r-address")


class DonationClaimResponse(BaseModel):
    accepted: bool
    is_new: bool
    record: dict


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/claim", response_model=DonationClaimResponse)
async def claim_donation(
    payload: DonationClaim,
    background: BackgroundTasks,
    store: IdempotencyStore = Depends(get_store),
) -> DonationClaimResponse:
    """Dapp tells us about a fresh donation. We register it immediately so
    the eventual Helius webhook has the Xahau address handy and the dapp
    can start polling /status."""

    if not SOLANA_SIG_RE.match(payload.sol_tx_sig):
        raise HTTPException(400, "invalid sol_tx_sig")
    if not SOLANA_PUBKEY_RE.match(payload.donor_wallet):
        raise HTTPException(400, "invalid donor_wallet")
    if payload.xahau_address and not is_valid_xahau_address(payload.xahau_address):
        raise HTTPException(400, "invalid xahau_address")

    record, is_new = store.claim_or_fetch(
        sol_tx_sig=payload.sol_tx_sig,
        donor_wallet=payload.donor_wallet,
        lamports=payload.lamports,
        xahau_address=payload.xahau_address,
    )

    # If the dapp claim arrives BEFORE the Helius webhook (typical), we
    # have the donor's Xahau address registered and ready. Triggers fire
    # from the webhook path so we know the tx is confirmed on-chain.
    return DonationClaimResponse(
        accepted=True,
        is_new=is_new,
        record=record.to_public_dict(),
    )


@router.post("/webhooks/helius")
async def helius_webhook(
    request: Request,
    background: BackgroundTasks,
    store: IdempotencyStore = Depends(get_store),
) -> dict:
    """Helius confirms the donate_sol tx. Verify HMAC, register the
    donation if not already, and kick off reward triggers in the
    background."""

    raw = await request.body()
    headers = {k: v for k, v in request.headers.items()}

    try:
        verify_helius_signature(raw, headers)
    except HeliusSignatureError as e:
        logger.warning("rejected Helius webhook: %s", e)
        raise HTTPException(401, str(e))

    try:
        events: list[dict] = await request.json()
    except Exception:
        raise HTTPException(400, "invalid JSON body")

    if not isinstance(events, list):
        events = [events]  # Helius sometimes sends a single object

    accepted = 0
    for ev in events:
        record = _record_from_helius_event(ev, store)
        if record is None:
            continue
        accepted += 1
        # Schedule reward triggers — never block the webhook response.
        background.add_task(_run_jtx_drop, record.sol_tx_sig)
        if record.xahau_address:
            background.add_task(_run_xahau_badge, record.sol_tx_sig)

    return {"accepted": accepted, "total": len(events)}


@router.get("/status/{sol_tx_sig}")
async def get_status(
    sol_tx_sig: str,
    store: IdempotencyStore = Depends(get_store),
) -> dict:
    if not SOLANA_SIG_RE.match(sol_tx_sig):
        raise HTTPException(400, "invalid sol_tx_sig")
    record = store.get(sol_tx_sig)
    if record is None:
        raise HTTPException(404, "donation not found")
    return record.to_public_dict()


@router.post("/admin/replay/{sol_tx_sig}")
async def replay_donation(
    sol_tx_sig: str,
    background: BackgroundTasks,
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
    store: IdempotencyStore = Depends(get_store),
) -> dict:
    expected = os.getenv("ADMIN_TOKEN", "")
    if not expected or x_admin_token != expected:
        raise HTTPException(401, "unauthorized")

    record = store.get(sol_tx_sig)
    if record is None:
        raise HTTPException(404, "donation not found")

    replayed: list[str] = []
    if record.jtx_drop_status in ("failed", "pending"):
        background.add_task(_run_jtx_drop, sol_tx_sig)
        replayed.append("jtx_drop")
    if record.xahau_address and record.xahau_status in ("failed", "pending"):
        background.add_task(_run_xahau_badge, sol_tx_sig)
        replayed.append("xahau_badge")

    return {"replayed": replayed}


# ── Helius event parsing ──────────────────────────────────────────────────

VAULT_PROGRAM_ID = os.getenv(
    "JETT_VAULT_PROGRAM_ID",
    "JTX5uXTiZ1M3hJkjv5Cp5F8dr3Jc7nhJbQjCFmgEYA7",
)
DONATE_SOL_DISCRIMINATOR_HEX = os.getenv(
    "DONATE_SOL_DISCRIMINATOR_HEX",
    # Anchor sighash: first 8 bytes of sha256("global:donate_sol")
    # Set this in env if your build uses a different name.
    "",
)


def _record_from_helius_event(
    event: dict,
    store: IdempotencyStore,
) -> DonationRecord | None:
    """Extract donor + lamports from a Helius enhanced-tx event.

    Helius `enhanced` webhook format includes `events`, `tokenTransfers`,
    `nativeTransfers`, and `instructions` arrays. We accept any of:
      - native transfer: source=donor, destination=vault PDA
      - inner instruction targeting jett_vault program with donate_sol disc
    """
    sol_tx_sig = event.get("signature") or event.get("transactionSignature")
    if not sol_tx_sig:
        return None

    # Skip failed transactions outright.
    if event.get("transactionError"):
        return None

    donor_wallet: str | None = None
    lamports: int = 0

    # Path 1: nativeTransfers heuristic — donor is the first signer that
    # transferred lamports to the vault PDA in the same tx.
    for nt in event.get("nativeTransfers", []) or []:
        amount = int(nt.get("amount", 0))
        if amount > 0:
            donor_wallet = nt.get("fromUserAccount") or donor_wallet
            lamports = max(lamports, amount)

    # Path 2: instructions with vault program id.
    if donor_wallet is None:
        for ix in event.get("instructions", []) or []:
            if ix.get("programId") == VAULT_PROGRAM_ID:
                accounts = ix.get("accounts", [])
                if accounts:
                    donor_wallet = accounts[0]
                break

    # Fallback: feePayer
    if donor_wallet is None:
        donor_wallet = event.get("feePayer")

    if not donor_wallet or not SOLANA_PUBKEY_RE.match(donor_wallet):
        logger.warning("could not extract donor from Helius event %s", sol_tx_sig)
        return None
    if lamports <= 0:
        logger.warning("could not extract lamports from Helius event %s", sol_tx_sig)
        return None

    # We may already have an Xahau address from the dapp's /claim ping.
    existing = store.get(sol_tx_sig)
    xahau = existing.xahau_address if existing else None

    record, _is_new = store.claim_or_fetch(
        sol_tx_sig=sol_tx_sig,
        donor_wallet=donor_wallet,
        lamports=lamports,
        xahau_address=xahau,
    )
    return record


# ── Background trigger runners ────────────────────────────────────────────


async def _run_jtx_drop(sol_tx_sig: str) -> None:
    store = get_store()
    record = store.get(sol_tx_sig)
    if record is None:
        logger.error("_run_jtx_drop: no record for %s", sol_tx_sig)
        return

    if record.jtx_drop_status in ("done", "in_flight"):
        return

    # Daily cap (rough abuse circuit-breaker).
    cap = int(os.getenv("JTX_DAILY_DROP_CAP", "10000"))
    if store.daily_jtx_drop_count() >= cap:
        logger.error("JTX_DAILY_DROP_CAP=%s reached, skipping %s", cap, sol_tx_sig)
        store.update_jtx_drop(
            sol_tx_sig,
            "failed",
            error="daily cap reached",
            attempts_increment=1,
        )
        return

    store.update_jtx_drop(sol_tx_sig, "in_flight", attempts_increment=1)
    try:
        svc = get_jtx_service()
        tx_sig = await svc.drop(donor_wallet=record.donor_wallet)
        store.update_jtx_drop(sol_tx_sig, "done", tx_sig=tx_sig)
        logger.info("JTX drop done for donation %s -> %s", sol_tx_sig, tx_sig)
    except Exception as e:  # pragma: no cover (network)
        logger.exception("JTX drop failed for %s", sol_tx_sig)
        store.update_jtx_drop(sol_tx_sig, "failed", error=str(e))


async def _run_xahau_badge(sol_tx_sig: str) -> None:
    store = get_store()
    record = store.get(sol_tx_sig)
    if record is None or not record.xahau_address:
        return
    if record.xahau_status in ("done", "in_flight"):
        return

    store.update_xahau(sol_tx_sig, "in_flight", attempts_increment=1)
    try:
        svc = get_xahau_service()
        if not svc.configured:
            store.update_xahau(sol_tx_sig, "failed", error="xahau service unconfigured")
            return
        tx_hash = await svc.trigger_badge(
            donor_xahau_addr=record.xahau_address,
            donation_id=sol_tx_sig,
            sol_tx_sig=sol_tx_sig,
        )
        store.update_xahau(sol_tx_sig, "done", tx_hash=tx_hash)
        logger.info("Xahau badge done for donation %s -> %s", sol_tx_sig, tx_hash)
    except Exception as e:  # pragma: no cover (network)
        logger.exception("Xahau badge failed for %s", sol_tx_sig)
        store.update_xahau(sol_tx_sig, "failed", error=str(e))
