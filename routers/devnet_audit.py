"""
Devnet-only AARON-audit self-serve endpoint for the astroknots.space vault
NFT-mint flow.

Mounted at `/audit` from aaron_router.py.

Routes:
  POST /audit/devnet
       Body: { "user_wallet": "<base58 pubkey>" }
       Creates or refreshes an aaron_audit PDA for the given user's AGT
       attestation on api.devnet.solana.com. The AARON operator signer is
       AARON's own Solana keypair (~/.config/solana/id.json on Jetson).
       Returns: { sig, action: "created" | "refreshed", audit_pda }

       This endpoint is DEVNET-ONLY by design. Mainnet's first-gaze flow
       remains exclusive to MOJO iOS (real ARKit gaze attestation) — this
       is only here so smoke-tests + devnet UX work end-to-end without
       requiring the iOS app.

  GET  /audit/devnet/status/{user_wallet}
       Returns the on-chain state of the AGT + audit PDAs for a wallet so
       the vault page can render appropriate "Bootstrap" vs "Refresh"
       vs "Ready to mint" CTAs.

Guards (belt + suspenders):
  - Env var DEVNET_AUDIT_ENABLED must be truthy (off by default in prod)
  - Cluster genesis hash must match the known devnet genesis
  - The user must have already created their AGT (we don't sign as them)
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solders.hash import Hash
from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.system_program import ID as SYSTEM_PROGRAM_ID
from solders.transaction import Transaction

logger = logging.getLogger("aaron.devnet_audit")

# ─── Constants ────────────────────────────────────────────────────────────────

VAULT_PROGRAM_ID = Pubkey.from_string("JTX5uXTiZ1M3hJkjv5Cp5F8dr3Jc7nhJbQjCFmgEYA7")

# Devnet genesis hash — sanity check against accidental mainnet exposure.
# https://docs.solana.com/clusters#devnet — fixed at network bring-up.
DEVNET_GENESIS_HASH = "EtWTRABZaYq6iMfeYKouRu166VU2xqa1wcaWoxPkrZBG"

DEVNET_RPC = os.environ.get(
    "DEVNET_RPC_URL", "https://api.devnet.solana.com"
)
AARON_KEYPAIR_PATH = os.environ.get(
    "AARON_KEYPAIR_PATH", str(Path.home() / ".config/solana/id.json")
)
DEVNET_AUDIT_ENABLED = os.environ.get("DEVNET_AUDIT_ENABLED", "").lower() in (
    "1",
    "true",
    "yes",
)

# Anchor instruction sighash = first 8 bytes of sha256("global:<name>")
def _sighash(name: str) -> bytes:
    return hashlib.sha256(f"global:{name}".encode()).digest()[:8]


SIGHASH_AARON_AUDIT = _sighash("aaron_audit")
SIGHASH_REFRESH_AARON_AUDIT = _sighash("refresh_aaron_audit")


# ─── Models ───────────────────────────────────────────────────────────────────


class DevnetAuditRequest(BaseModel):
    user_wallet: str  # base58 pubkey of the donor whose AGT we're auditing


class DevnetAuditResponse(BaseModel):
    sig: str
    action: str  # "created" | "refreshed"
    audit_pda: str
    agt_pda: str


class DevnetAuditStatus(BaseModel):
    user_wallet: str
    agt_exists: bool
    audit_exists: bool
    audit_age_seconds: Optional[int] = None
    audit_pda: str
    agt_pda: str


# ─── Router ───────────────────────────────────────────────────────────────────


router = APIRouter(prefix="/audit", tags=["devnet-audit"])


def _load_aaron_keypair() -> Keypair:
    """Load AARON's signing keypair from disk."""
    import json

    with open(AARON_KEYPAIR_PATH) as f:
        secret = json.load(f)
    return Keypair.from_bytes(bytes(secret))


def _derive_pdas(user: Pubkey) -> tuple[Pubkey, Pubkey, Pubkey]:
    """Return (vault_config, agt_attestation, aaron_audit) PDAs for user."""
    vault, _ = Pubkey.find_program_address([b"vault_config"], VAULT_PROGRAM_ID)
    agt, _ = Pubkey.find_program_address(
        [b"agt_attestation", bytes(user)], VAULT_PROGRAM_ID
    )
    audit, _ = Pubkey.find_program_address(
        [b"aaron_audit", bytes(agt)], VAULT_PROGRAM_ID
    )
    return vault, agt, audit


async def _ensure_devnet(client: AsyncClient) -> None:
    """Belt-and-suspenders: confirm we're talking to devnet, not mainnet."""
    if not DEVNET_AUDIT_ENABLED:
        raise HTTPException(
            status_code=503,
            detail="Devnet audit endpoint disabled — set DEVNET_AUDIT_ENABLED=1.",
        )
    resp = await client.get_genesis_hash()
    if str(resp.value) != DEVNET_GENESIS_HASH:
        raise HTTPException(
            status_code=403,
            detail=f"Cluster genesis hash {resp.value} does not match devnet — refusing to sign.",
        )


def _build_audit_data(
    sighash: bytes,
    audit_hash: bytes,
    risk_score: int,
    cog_score: int,
    env_score: int,
    emo_score: int,
    audit_notes_hash: bytes,
) -> bytes:
    """Borsh-encode args for aaron_audit / refresh_aaron_audit.

    Layout (matches programs/jett-vault/src/lib.rs):
      [u8;8] discriminator
      [u8;32] audit_hash
      u16 risk_score
      u16 cog_score
      u16 env_score
      u16 emo_score
      [u8;32] audit_notes_hash
    """
    if len(audit_hash) != 32 or len(audit_notes_hash) != 32:
        raise ValueError("hash fields must be 32 bytes")
    return (
        sighash
        + audit_hash
        + risk_score.to_bytes(2, "little")
        + cog_score.to_bytes(2, "little")
        + env_score.to_bytes(2, "little")
        + emo_score.to_bytes(2, "little")
        + audit_notes_hash
    )


def _build_audit_ix(
    sighash: bytes,
    aaron_operator: Pubkey,
    user_wallet: Pubkey,
    vault_pda: Pubkey,
    agt_pda: Pubkey,
    audit_pda: Pubkey,
    *,
    is_init: bool,
    audit_hash: bytes,
    risk_score: int,
    cog_score: int,
    env_score: int,
    emo_score: int,
    audit_notes_hash: bytes,
) -> Instruction:
    """Build the aaron_audit or refresh_aaron_audit instruction.

    The two ixs share the same arg layout but have different account meta
    (init writes vault_config + system_program; refresh just reads them).
    """
    keys = [
        AccountMeta(pubkey=aaron_operator, is_signer=True, is_writable=True),
        AccountMeta(pubkey=agt_pda, is_signer=False, is_writable=is_init),
        AccountMeta(pubkey=audit_pda, is_signer=False, is_writable=True),
        AccountMeta(pubkey=vault_pda, is_signer=False, is_writable=is_init),
    ]
    if is_init:
        keys.append(
            AccountMeta(
                pubkey=SYSTEM_PROGRAM_ID, is_signer=False, is_writable=False
            )
        )

    data = _build_audit_data(
        sighash,
        audit_hash,
        risk_score,
        cog_score,
        env_score,
        emo_score,
        audit_notes_hash,
    )
    return Instruction(VAULT_PROGRAM_ID, data, keys)


@router.post("/devnet", response_model=DevnetAuditResponse)
async def devnet_audit(req: DevnetAuditRequest) -> DevnetAuditResponse:
    """Create or refresh an AARON audit PDA for a user wallet on devnet."""
    try:
        user_wallet = Pubkey.from_string(req.user_wallet)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid pubkey: {exc}") from exc

    async with AsyncClient(DEVNET_RPC, commitment=Confirmed) as client:
        await _ensure_devnet(client)

        vault_pda, agt_pda, audit_pda = _derive_pdas(user_wallet)

        # The user must have already created their AGT themselves (only they
        # can sign create_agt_attestation since the PDA seed is their pubkey).
        agt_info = await client.get_account_info(agt_pda, commitment=Confirmed)
        if agt_info.value is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "AGT attestation does not exist for this wallet. The user "
                    "must call create_agt_attestation themselves first (devnet "
                    "bootstrap button on the vault page handles this)."
                ),
            )

        audit_info = await client.get_account_info(audit_pda, commitment=Confirmed)
        is_init = audit_info.value is None
        action = "created" if is_init else "refreshed"

        # Synthetic audit metadata for devnet — clearly tagged so anyone
        # inspecting on-chain history can tell these apart from real audits.
        audit_hash = hashlib.sha256(
            bytes(agt_pda) + b"aaron-devnet-bootstrap-2026-05-10"
        ).digest()
        audit_notes_hash = hashlib.sha256(
            f"devnet-bootstrap action={action} wallet={user_wallet}".encode()
        ).digest()

        keypair = _load_aaron_keypair()
        sighash = SIGHASH_AARON_AUDIT if is_init else SIGHASH_REFRESH_AARON_AUDIT
        ix = _build_audit_ix(
            sighash,
            keypair.pubkey(),
            user_wallet,
            vault_pda,
            agt_pda,
            audit_pda,
            is_init=is_init,
            audit_hash=audit_hash,
            risk_score=100,  # 1.00% — synthetic low risk
            cog_score=100,
            env_score=100,
            emo_score=100,
            audit_notes_hash=audit_notes_hash,
        )

        recent = await client.get_latest_blockhash(commitment=Confirmed)
        message = Message.new_with_blockhash(
            [ix], keypair.pubkey(), recent.value.blockhash
        )
        tx = Transaction([keypair], message, recent.value.blockhash)

        resp = await client.send_transaction(
            tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
        )
        sig = str(resp.value)
        # Wait for confirmation so the next call sees the new state.
        await client.confirm_transaction(resp.value, commitment=Confirmed)

        logger.info(
            "[devnet-audit] %s audit for wallet=%s sig=%s",
            action,
            req.user_wallet,
            sig,
        )

        return DevnetAuditResponse(
            sig=sig,
            action=action,
            audit_pda=str(audit_pda),
            agt_pda=str(agt_pda),
        )


@router.get("/devnet/status/{user_wallet}", response_model=DevnetAuditStatus)
async def devnet_audit_status(user_wallet: str) -> DevnetAuditStatus:
    """Return on-chain state so the vault page can render correct CTAs."""
    try:
        user_pk = Pubkey.from_string(user_wallet)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid pubkey: {exc}") from exc

    async with AsyncClient(DEVNET_RPC, commitment=Confirmed) as client:
        await _ensure_devnet(client)
        _vault, agt_pda, audit_pda = _derive_pdas(user_pk)

        agt_info = await client.get_account_info(agt_pda, commitment=Confirmed)
        audit_info = await client.get_account_info(audit_pda, commitment=Confirmed)

        audit_age: Optional[int] = None
        if audit_info.value is not None:
            # AaronAuditAccount layout: 8-byte Anchor discriminator, then:
            #   agt_attestation: Pubkey  (32)
            #   auditor:         Pubkey  (32)
            #   audit_hash:      [u8;32] (32)
            #   risk_score:      u16     (2)
            #   cog/env/emo:     u16*3   (6)
            #   audit_notes_hash:[u8;32] (32)
            #   audited_at:      i64     (8)
            data = bytes(audit_info.value.data)
            # offset to audited_at = 8 + 32 + 32 + 32 + 2 + 2 + 2 + 2 + 32 = 144
            audited_at = int.from_bytes(data[144:152], "little", signed=True)
            import time

            audit_age = max(0, int(time.time()) - audited_at)

        return DevnetAuditStatus(
            user_wallet=user_wallet,
            agt_exists=agt_info.value is not None,
            audit_exists=audit_info.value is not None,
            audit_age_seconds=audit_age,
            audit_pda=str(audit_pda),
            agt_pda=str(agt_pda),
        )
