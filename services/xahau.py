"""
Phase 1 — Xahau cross-chain donor badge trigger.

When the dapp captures a donor's Xahau r-address alongside a SOL
donation, the router pays a 5-XAH micro-payment to the deployed
NFT-mint Hook account. The Hook recognizes the trigger and mints a
URIToken donor badge addressed to the donor's Xahau wallet, with the
Solana donation tx signature embedded in the metadata.

This service is the "trigger" half — the Hook itself lives on-chain
on Xahau and is configured via builder.xahau.network. See
docs/xahau-hook-deployment.md for the deploy walkthrough.

The xrpl-py dependency is optional in CI — we lazy-import it inside
`__init__` so unit tests don't require the wheel.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any


logger = logging.getLogger("aaron.xahau")


@dataclass(frozen=True)
class XahauConfig:
    rpc_url: str
    funding_seed: str
    hook_account: str
    donation_amount_drops: int  # 5 XAH = 5_000_000 drops

    @classmethod
    def from_env(cls) -> "XahauConfig":
        seed = os.getenv("XAHAU_FUNDING_SEED", "")
        hook = os.getenv("XAHAU_HOOK_ACCOUNT", "")
        rpc = os.getenv("XAHAU_RPC_URL", "wss://xahau.network")
        amt = int(os.getenv("XAHAU_DONATION_AMOUNT_DROPS", "5000000"))

        if not seed or not hook:
            raise RuntimeError(
                "XAHAU_FUNDING_SEED and XAHAU_HOOK_ACCOUNT must be set"
            )
        return cls(
            rpc_url=rpc,
            funding_seed=seed,
            hook_account=hook,
            donation_amount_drops=amt,
        )


def _build_memos(donor_xahau_addr: str, donation_id: str, sol_tx_sig: str) -> list[dict]:
    """Construct the Memos list the Hook reads from.

    Memo encoding rules per XLS-15:
      - MemoData hex-encodes the bytes
      - MemoType is a hint to the Hook ("recipient" / "donation_id" / "sol_tx")
    """
    def encode(s: str) -> str:
        return s.encode("utf-8").hex().upper()

    return [
        {
            "Memo": {
                "MemoType": encode("recipient"),
                "MemoData": encode(donor_xahau_addr),
            }
        },
        {
            "Memo": {
                "MemoType": encode("donation_id"),
                "MemoData": encode(donation_id),
            }
        },
        {
            "Memo": {
                "MemoType": encode("sol_tx"),
                "MemoData": encode(sol_tx_sig),
            }
        },
    ]


class XahauBadgeService:
    """Triggers the Xahau NFT-mint Hook by paying it 5 XAH with memos."""

    def __init__(self, config: XahauConfig | None = None) -> None:
        self._config: XahauConfig | None = None
        self._wallet: Any = None
        self._client: Any = None

        if config is not None:
            self._config = config
        else:
            # Defer env loading until first use — XAHAU vars may not be
            # set during local dev that doesn't exercise this path.
            try:
                self._config = XahauConfig.from_env()
            except RuntimeError as e:
                logger.warning("Xahau service is unconfigured: %s", e)

    @property
    def configured(self) -> bool:
        return self._config is not None

    def _ensure_clients(self) -> None:
        if self._wallet is not None and self._client is not None:
            return
        if self._config is None:
            raise RuntimeError("Xahau service not configured (set XAHAU_* env vars)")

        # Lazy import — keeps the package importable on hosts that haven't
        # yet `pip install xrpl-py`.
        from xrpl.wallet import Wallet
        from xrpl.asyncio.clients import AsyncWebsocketClient

        self._wallet = Wallet.from_seed(self._config.funding_seed)
        self._client = AsyncWebsocketClient(self._config.rpc_url)

    async def trigger_badge(
        self,
        donor_xahau_addr: str,
        donation_id: str,
        sol_tx_sig: str,
    ) -> str:
        """Pay 5 XAH to the Hook with memos. Returns Xahau tx hash.

        On failure, raises. Caller should mark the leg `failed`.
        """
        if self._config is None:
            raise RuntimeError("Xahau service not configured")
        self._ensure_clients()

        # Lazy imports continued.
        from xrpl.models.transactions import Payment
        from xrpl.asyncio.transaction import autofill_and_sign, submit_and_wait

        cfg = self._config
        memos = _build_memos(donor_xahau_addr, donation_id, sol_tx_sig)

        async with self._client as client:
            payment = Payment(
                account=self._wallet.classic_address,
                destination=cfg.hook_account,
                amount=str(cfg.donation_amount_drops),
                memos=memos,  # type: ignore[arg-type]
            )
            signed = await autofill_and_sign(payment, client, self._wallet)
            response = await submit_and_wait(signed, client, self._wallet)
            tx_hash = response.result.get("hash") or signed.get_hash()
            logger.info(
                "Xahau badge triggered: hook=%s recipient=%s tx=%s",
                cfg.hook_account,
                donor_xahau_addr,
                tx_hash,
            )
            return str(tx_hash)


def is_valid_xahau_address(addr: str) -> bool:
    """Light validation for r-addresses. The Hook does final validation."""
    if not addr or not addr.startswith("r"):
        return False
    if len(addr) < 25 or len(addr) > 35:
        return False
    # base58 alphabet (ripple flavor — no 0OIl)
    allowed = set("rpshnaf39wBUDNEGHJKLM4PQRST7VWXYZ2bcdeCg65jkm8oFqi1tuvAxyz")
    return all(c in allowed for c in addr)


__all__ = [
    "XahauBadgeService",
    "XahauConfig",
    "is_valid_xahau_address",
]
