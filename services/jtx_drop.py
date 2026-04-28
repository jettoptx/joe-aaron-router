"""
Phase 3a — Instant 1 JTX SPL drop.

Sends `JTX_DROP_AMOUNT` whole tokens of JTX (Token-2022) from the
JOE agent wallet to the donor's wallet ATA. Idempotency is enforced
by the caller via `IdempotencyStore` — this module is concerned only
with building, signing, and submitting the transfer.

Network model:
- Mainnet: JTX mint = `9XpJiKEYzq5yDo5pJzRfjSRMPL2yPfDQXgiN7uYtBhUj`
- Devnet/local: cloned mint via `solana-test-validator --clone <mainnet>`,
  configured via Anchor.toml's [[test.validator.clone]] block.
- Both networks use TOKEN_2022_PROGRAM_ID
  (`TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb`).

The JOE agent keypair is loaded from disk at startup (file mode 0400,
owned by the systemd user). We refuse to start if the file is
world-readable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from solders.keypair import Keypair
from solders.pubkey import Pubkey
from solders.transaction import Transaction
from solders.message import Message
from solders.instruction import Instruction
from solders.system_program import ID as SYSTEM_PROGRAM_ID

from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

from spl.token.constants import TOKEN_2022_PROGRAM_ID, ASSOCIATED_TOKEN_PROGRAM_ID
from spl.token.instructions import (
    get_associated_token_address,
    create_associated_token_account,
    transfer_checked,
    TransferCheckedParams,
)


logger = logging.getLogger("aaron.jtx_drop")


# ── Config ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class JTXDropConfig:
    rpc_url: str
    keypair_path: str
    jtx_mint: Pubkey
    decimals: int
    drop_amount_whole: int  # whole tokens to send per donation

    @classmethod
    def from_env(cls) -> "JTXDropConfig":
        network = os.getenv("SOLANA_NETWORK", "mainnet").lower()
        if network.startswith("dev") or network == "localnet":
            mint_str = os.getenv("JTX_MINT_DEVNET", "")
            rpc = os.getenv("SOLANA_RPC_URL_DEVNET") or os.getenv("SOLANA_RPC_URL", "")
        else:
            mint_str = os.getenv(
                "JTX_MINT_MAINNET",
                "9XpJiKEYzq5yDo5pJzRfjSRMPL2yPfDQXgiN7uYtBhUj",
            )
            rpc = os.getenv("SOLANA_RPC_URL", "")

        if not mint_str:
            raise RuntimeError(
                f"JTX_MINT_{'DEVNET' if 'dev' in network else 'MAINNET'} env var is required"
            )
        if not rpc:
            raise RuntimeError("SOLANA_RPC_URL env var is required")

        keypair_path = os.getenv("JOE_AGENT_KEYPAIR_PATH", "/etc/optx/joe-agent.json")

        return cls(
            rpc_url=rpc,
            keypair_path=keypair_path,
            jtx_mint=Pubkey.from_string(mint_str),
            decimals=int(os.getenv("JTX_DECIMALS", "9")),
            drop_amount_whole=int(os.getenv("JTX_DROP_AMOUNT", "1")),
        )


# ── Keypair loader ────────────────────────────────────────────────────────

def load_agent_keypair(path: str) -> Keypair:
    """Load the JOE agent keypair from disk.

    Refuses if the file is world- or group-readable, matching `solana-keygen`'s
    safety convention. Acceptable modes: 0400, 0600.
    """
    p = Path(path)
    if not p.exists():
        raise RuntimeError(f"JOE agent keypair not found at {path}")

    mode = p.stat().st_mode
    # Reject group/world readable bits.
    if mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH):
        raise RuntimeError(
            f"JOE agent keypair at {path} has unsafe permissions "
            f"({oct(stat.S_IMODE(mode))}); chmod 0400"
        )

    raw = json.loads(p.read_text())
    if not isinstance(raw, list) or len(raw) != 64:
        raise RuntimeError(
            f"JOE agent keypair at {path} is malformed "
            "(expected 64-byte JSON array)"
        )
    return Keypair.from_bytes(bytes(raw))


# ── Service ───────────────────────────────────────────────────────────────

class JTXDropService:
    """Transfers JTX (Token-2022) from JOE agent to donor wallet."""

    def __init__(self, config: JTXDropConfig | None = None) -> None:
        self._config = config or JTXDropConfig.from_env()
        self._keypair: Keypair | None = None
        self._lock = asyncio.Lock()

    @property
    def config(self) -> JTXDropConfig:
        return self._config

    def _ensure_keypair(self) -> Keypair:
        if self._keypair is None:
            self._keypair = load_agent_keypair(self._config.keypair_path)
            logger.info(
                "loaded JOE agent keypair: %s",
                self._keypair.pubkey(),
            )
        return self._keypair

    async def drop(
        self,
        donor_wallet: str,
    ) -> str:
        """Send `drop_amount_whole` JTX to donor_wallet. Returns tx signature.

        Idempotency is the caller's responsibility — this method always
        attempts a fresh transfer. The caller should:
          1. claim the donation row in IdempotencyStore (raises if dupe),
          2. call drop(),
          3. record the returned tx_sig.

        On failure, raises. The caller should mark the leg `failed` and
        decide whether to retry (with bounded attempts).
        """
        cfg = self._config
        donor = Pubkey.from_string(donor_wallet)
        sender = self._ensure_keypair()

        sender_ata = get_associated_token_address(
            owner=sender.pubkey(),
            mint=cfg.jtx_mint,
            token_program_id=TOKEN_2022_PROGRAM_ID,
        )
        donor_ata = get_associated_token_address(
            owner=donor,
            mint=cfg.jtx_mint,
            token_program_id=TOKEN_2022_PROGRAM_ID,
        )

        # Serialize tx-building per process to avoid blockhash races on the
        # rare burst of webhooks for the same agent wallet.
        async with self._lock:
            async with AsyncClient(cfg.rpc_url) as client:
                ixs: list[Instruction] = []

                # Create donor ATA if missing. This is paid by the agent
                # wallet (the fee payer + signer of this tx).
                resp = await client.get_account_info(donor_ata, commitment=Confirmed)
                if resp.value is None:
                    ixs.append(
                        create_associated_token_account(
                            payer=sender.pubkey(),
                            owner=donor,
                            mint=cfg.jtx_mint,
                            token_program_id=TOKEN_2022_PROGRAM_ID,
                        )
                    )
                    logger.info("creating donor ATA %s for wallet %s", donor_ata, donor)

                amount_base = cfg.drop_amount_whole * (10 ** cfg.decimals)
                ixs.append(
                    transfer_checked(
                        TransferCheckedParams(
                            program_id=TOKEN_2022_PROGRAM_ID,
                            source=sender_ata,
                            mint=cfg.jtx_mint,
                            dest=donor_ata,
                            owner=sender.pubkey(),
                            amount=amount_base,
                            decimals=cfg.decimals,
                            signers=[],
                        )
                    )
                )

                blockhash_resp = await client.get_latest_blockhash(commitment=Confirmed)
                blockhash = blockhash_resp.value.blockhash

                msg = Message.new_with_blockhash(ixs, sender.pubkey(), blockhash)
                tx = Transaction.new_unsigned(msg)
                tx.sign([sender], blockhash)

                send_resp = await client.send_raw_transaction(
                    bytes(tx),
                    opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed),
                )
                tx_sig = str(send_resp.value)

                # Confirm with a generous timeout — we trade latency for
                # certainty in the audit log.
                await client.confirm_transaction(
                    send_resp.value,
                    commitment=Confirmed,
                    sleep_seconds=1.0,
                )
                logger.info(
                    "JTX drop confirmed: %s -> %s tx=%s",
                    sender.pubkey(),
                    donor,
                    tx_sig,
                )
                return tx_sig


__all__ = ["JTXDropService", "JTXDropConfig", "load_agent_keypair"]
