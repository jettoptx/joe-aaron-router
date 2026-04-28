# Xahau Hook Deployment — Genesis Donor Badge

Phase 1 of the donor reward stack: cross-chain "JTX Liquidity Donor #N
– Genesis" URIToken minted on Xahau when AARON pays the Hook account
5 XAH with the donor's r-address in `Memos[0]`.

This doc covers the **on-Xahau** half. The router-side trigger is
already implemented in `services/xahau.py` — once you fill in the env
vars below, restart AARON and the Xahau leg fires automatically when
a donor provides their r-address.

---

## Prerequisites

- ~50 XAH in a funded Xahau account ("the Hook account")
- ~10 XAH in a separate funded Xahau account ("the funding account",
  same as router's `XAHAU_FUNDING_SEED`)
- Xaman (mobile) installed for testing
- A 512×512 PNG for the badge image, pinned to IPFS via Pinata or
  nft.storage. Keep the CID — it goes into the Hook's metadata
  template.

If you only have XRP today, bridge ~50 XAH via Magnetic DEX (XRPL → XAH):

1. Open https://magnetic.network in a browser, connect Xaman.
2. Pay 50 XAH from the XRPL wallet via the bridge (cost: ~0.001 XRP).
3. Bridged XAH lands in your Xahau account within ~30s.

---

## Step 1 — Test on Xahau testnet first

Use https://xahau-test.net/faucet to fund a testnet account with 1000 XAH.

Update env vars on a staging copy of AARON:

```
XAHAU_RPC_URL=wss://xahau-test.network
XAHAU_FUNDING_SEED=sEdT...          # your testnet seed
XAHAU_HOOK_ACCOUNT=rTestHookAcct... # your testnet hook account (created below)
XAHAU_DONATION_AMOUNT_DROPS=5000000 # 5 XAH
```

---

## Step 2 — Deploy the NFT-Mint Hook

Open https://builder.xahau.network → "NFT Mint" template.

Configuration:

| Field | Value |
|---|---|
| **Hook Account** | the Hook account address |
| **Trigger condition** | `tt == ttPAYMENT && Amount == 5000000 && sfDestination == hookAccount` |
| **Sender allow-list** | the funding account address only — anyone else paying 5 XAH gets refunded |
| **Mint type** | `URITokenMint` |
| **Mint to** | parsed from `Memos[0].MemoData` (recipient r-address, hex-decoded) |
| **Token URI template** | `ipfs://{CID}/{counter}.json` where {CID} is your pinned metadata folder, or a static URI for a single shared metadata file |
| **Metadata fields** | name = `JTX Liquidity Donor #{counter} – Genesis`, sol_tx = `Memos[2].MemoData`, donation_id = `Memos[1].MemoData`, mint_time = `ledger_time` |
| **Counter source** | Hook State key `donor_counter`, increment +1 per mint |

Memo layout the router sends (see `services/xahau.py::_build_memos`):

| Index | MemoType (hex of UTF-8) | MemoData (hex of UTF-8) |
|---|---|---|
| 0 | `recipient` | donor's r-address |
| 1 | `donation_id` | Solana tx signature (used as idempotency key on-chain) |
| 2 | `sol_tx` | Solana tx signature (mirror — the Hook stores it in the URIToken) |

The Hook should:
1. Verify sender is the funding account.
2. Verify amount == 5000000 drops exactly.
3. Decode `Memos[0]` to recipient r-address.
4. Mint URIToken to recipient with the metadata template.
5. Increment `donor_counter` in Hook State.
6. (Optional) emit a `RewardMinted` transaction event.

Compile + deploy in builder.xahau.network. Capture the Hook account
address — this becomes `XAHAU_HOOK_ACCOUNT`.

---

## Step 3 — Smoke test on testnet

```bash
# Pretend to be AARON sending the trigger payment:
python -c "
from services.xahau import XahauBadgeService, XahauConfig
import asyncio
svc = XahauBadgeService(XahauConfig(
    rpc_url='wss://xahau-test.network',
    funding_seed='sEdT...',
    hook_account='rTestHookAcct...',
    donation_amount_drops=5000000,
))
print(asyncio.run(svc.trigger_badge('rTestRecipientAddr...', 'donation-id-123', 'soltx-abc')))
"
```

Expect: a tx hash printed within 5–10s. Check the Hook account on
https://test.xahauexplorer.com — you should see:
1. The 5-XAH payment from the funding account.
2. A URIToken minted to the recipient.

If the URIToken doesn't mint, the Hook rejected the payment — check
the Hook's emitted events for the rejection reason and adjust the
template.

---

## Step 4 — Promote to mainnet

Re-deploy the same Hook to a mainnet account:

```
XAHAU_RPC_URL=wss://xahau.network
XAHAU_FUNDING_SEED=<mainnet seed, sealed>
XAHAU_HOOK_ACCOUNT=<mainnet hook addr>
XAHAU_DONATION_AMOUNT_DROPS=5000000
```

Restart AARON. The next donation with a Xahau address attached will
mint a real Genesis donor badge.

---

## Operations

### Refill the funding account

5 XAH per donation. 1000 XAH funds 200 donations. Top up via Magnetic
when balance drops below 50 XAH.

### Disable the Xahau leg cleanly

Set `XAHAU_FUNDING_SEED=""` and restart AARON. The router's
`_run_xahau_badge` short-circuits to a `failed` status with reason
`xahau service unconfigured`. The JTX drop continues unaffected.

### Replay a failed badge

```bash
curl -X POST https://api.astroknots.space/donations/admin/replay/<sig> \
  -H 'x-admin-token: <ADMIN_TOKEN>'
```

Only the `xahau_badge` leg is replayed if `jtx_drop` is already done.

---

## Cost analysis

- 5 XAH per badge ≈ $0.0005–$0.001 at typical XAH price
- 1000 XAH ≈ 200 badges ≈ $0.10–$0.20 total network cost for the
  whole donor cohort
- One-time Hook deployment: ~10 XAH (gas + storage reserve)

Your 1000-XRP stack bridges to ~1000 XAH — enough for **~200,000+
donor badges** lifetime, easily.
