# Donor Reward Stack — Deploy Guide

End-to-end checklist for shipping the dual-NFT donor reward layer
(`POST /donations/*` routes) on AARON Router + the joe-jettopics-saas
Vercel dapp.

Phases shipped in v1:
- **Phase 2** — Helius webhook handler + idempotency
- **Phase 3a** — Instant 1 JTX SPL drop from JOE agent wallet
- **Phase 4** — Frontend Xahau-address input + status modal
- **Phase 1** — Xahau Hook trigger (deferred — service stub is ready;
  enable by setting `XAHAU_FUNDING_SEED` + `XAHAU_HOOK_ACCOUNT`)
- **Phase 3b** — Metaplex Core soulbound receipt (v1.1, not in this release)

---

## 1. Provision the JOE agent keypair on the Jetson

```bash
# On your local machine (where you have the keypair JSON):
scp joe-agent-keypair.json jettoptx@100.85.183.16:/tmp/joe-agent.json

# On the Jetson:
ssh jettoptx@100.85.183.16
sudo install -d -m 0750 -o jettoptx -g jettoptx /etc/optx
sudo install -m 0400 -o jettoptx -g jettoptx /tmp/joe-agent.json /etc/optx/joe-agent.json
shred -u /tmp/joe-agent.json
ls -la /etc/optx/joe-agent.json   # should be -r-------- 1 jettoptx jettoptx
```

The router refuses to start if the keypair is group/world readable —
this is a deliberate fail-fast.

Top up the JOE agent wallet (`EFvgELE1Hb4PC5tbPTAe8v1uEDGee8nwYBMCU42bZRGk`)
with ~0.5 SOL for ATA-creation and tx fees. Each drop costs roughly
0.0005 SOL when the donor's ATA already exists, ~0.002 SOL when the
router has to create it. 0.5 SOL ≈ 250 first-time-donor drops or
1000+ repeat-donor drops.

---

## 2. Provision SQLite + systemd

```bash
sudo install -d -m 0755 -o jettoptx -g jettoptx /var/lib/optx
```

Add to your existing AARON systemd unit (or create one):

```ini
[Service]
User=jettoptx
Group=jettoptx
EnvironmentFile=/etc/optx/aaron.env   # mode 0400
WorkingDirectory=/home/jettoptx/joe-aaron-router
ExecStart=/home/jettoptx/joe-aaron-router/.venv/bin/python aaron_router.py
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/optx
PrivateTmp=true
```

`/etc/optx/aaron.env` should be a `chmod 0400` file populated from
`.env.example` — copy the template, fill in secrets, **never commit it**.

---

## 3. Create the Helius webhook

On https://dashboard.helius.dev → Webhooks → Add Webhook:

- **Webhook URL**: `https://api.astroknots.space/donations/webhooks/helius`
  (or whatever public hostname terminates TLS in front of AARON)
- **Webhook Type**: Enhanced
- **Transaction Types**: select `ANY` (we filter by program ID server-side)
- **Account Addresses**:
  - `JTX5uXTiZ1M3hJkjv5Cp5F8dr3Jc7nhJbQjCFmgEYA7`  (jett-vault program)
  - `CQmBrff4a9MouY4eQTAPKXKprtvfHZDNSkNCQhRR38tP` (vault PDA — catches native transfers)
- **Auth Header**: optional but recommended — set to a long random string and
  paste the same value into `HELIUS_WEBHOOK_SECRET` on the router.

Save the secret. Test with `curl -X POST https://.../donations/webhooks/helius`
expecting a `401 missing Helius signature header` response.

---

## 4. Deploy AARON

```bash
cd /home/jettoptx/joe-aaron-router
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart aaron-router
sudo systemctl status aaron-router
journalctl -u aaron-router -f
```

Smoke check:

```bash
curl -s http://localhost:8888/health | jq
# expect status=healthy

curl -s -X POST http://localhost:8888/donations/claim \
  -H 'content-type: application/json' \
  -d '{"sol_tx_sig":"5j7s'$(printf '1%.0s' {1..70})'","donor_wallet":"EFvgELE1Hb4PC5tbPTAe8v1uEDGee8nwYBMCU42bZRGk","lamports":500000000}' | jq
# expect accepted=true, is_new=true

curl -s -X POST http://localhost:8888/donations/webhooks/helius -H 'content-type: application/json' -d '[]'
# expect 401 (missing signature)
```

---

## 5. Configure Vercel (joe-jettopics-saas)

In https://vercel.com/space-cowboys/joe-jettopics-saas → Settings → Environment Variables:

| Key | Value | Scope |
|---|---|---|
| `AARON_BASE_URL` | `https://api.astroknots.space` (your AARON public URL) | Production + Preview |
| `BACKEND_PROXY_TOKEN` | random 32+ char string (matches AARON's `BACKEND_PROXY_TOKEN`) | sealed |

The frontend never reads either of those — they're consumed only by
`app/api/donor-claim/route.ts` and `app/api/donor-status/[txsig]/route.ts`.

Trigger a redeploy. Test the preview URL by donating 0.5 SOL on a
test wallet and watching the success modal status panel update.

---

## 6. End-to-end verification

### 6a. Devnet smoke (run before mainnet)

```bash
# From a throwaway donor wallet:
solana airdrop 1 -u devnet
# In the dapp (run dev server with NEXT_PUBLIC_SOLANA_NETWORK=devnet):
#   donate 0.1 SOL, paste a Xahau testnet r-address.
```

Expect within 15 s:
- AARON logs: `JTX drop confirmed: ... -> <tx_sig>`
- Donor's wallet receives 1 JTX (verify with `spl-token accounts -u devnet`)
- Status modal shows `Delivered` for "1 JTX Genesis Token"

If `XAHAU_*` env vars are set, expect within 30 s:
- AARON logs: `Xahau badge triggered: hook=... recipient=... tx=...`
- URIToken visible at https://xahau-test.net/account/{XAHAU_HOOK_ACCOUNT}
- Status modal shows `Delivered` for "Xahau Donor Badge"

### 6b. Mainnet smoke

Same flow with smallest preset (0.5 SOL). Confirm:
- Helius webhook logs show 200 OK delivery
- AARON `/donations/status/{tx_sig}` returns `done` for jtx_drop
- Vault stats UI shows the donation
- Helius webhook delivery dashboard shows clean delivery (no retries needed)

### 6c. Idempotency check

Replay the Helius webhook payload manually:

```bash
curl -X POST https://api.astroknots.space/donations/webhooks/helius \
  -H 'content-type: application/json' \
  -H 'x-helius-signature: <recompute>' \
  -d @last_payload.json
```

Expect: AARON logs `JTX drop done` only once. The replayed delivery
should hit the in-flight/done early-return path, not double-pay.

---

## 7. Operations

### Manual replay of a failed leg

```bash
curl -X POST https://api.astroknots.space/donations/admin/replay/<sig> \
  -H 'x-admin-token: <ADMIN_TOKEN>'
```

### Check daily JTX-drop usage

```bash
sqlite3 /var/lib/optx/donations.db \
  "SELECT COUNT(*) FROM donations WHERE created_at >= strftime('%s','now')-86400 AND jtx_drop_status='done';"
```

### Bump the daily cap

Edit `JTX_DAILY_DROP_CAP` in `/etc/optx/aaron.env`, `systemctl restart aaron-router`.

### Rotate the JOE agent keypair

1. Generate new keypair, fund it from old keypair (transfer 1.1M JTX + 0.5 SOL).
2. `sudo install -m 0400 ... /etc/optx/joe-agent.json` (overwrite).
3. `sudo systemctl restart aaron-router`. Boot logs print the new pubkey.
4. Update `EFvgELE1Hb4PC5tbPTAe8v1uEDGee8nwYBMCU42bZRGk` references in
   `app/vault/page.tsx`, `app/api/donate/route.ts`, README, etc.
5. Drain & retire old wallet.

---

## 8. What the Vercel deployer / dapp dev needs to know

- **No keypair material ever lives in Vercel.** The browser bundle
  contains `JTX_MINT`, `JOE_PUBLIC_KEY`, and the AARON public URL —
  no secrets.
- **The dapp double-fires.** `registerDonationClaim()` runs in the
  browser to register the Xahau address before Helius confirms.
  Helius's webhook also registers/updates the row when the tx
  confirms. The router's idempotency layer makes this safe.
- **Failures don't roll back the donation.** A donation is on-chain
  the moment the donor signs it. Reward triggers run async; if they
  fail, donation is still safe and rewards are recoverable via
  `/donations/admin/replay`.

---

## 9. Known limitations / v1.1 follow-ups

- **Xahau Hook deployment** — see `docs/xahau-hook-deployment.md`
  (separate doc). Until deployed, set `XAHAU_FUNDING_SEED=""` to
  cleanly skip the leg.
- **Metaplex Core soulbound receipt** — Phase 3b. Requires a new
  `claim_donor_receipt` instruction in `jett-vault` + an upgrade.
  Tracked separately.
- **OFAC screening** — not implemented. Add at the `_run_jtx_drop`
  entrypoint if your jurisdiction requires it.
- **HSM signer** — current impl loads the keypair into RAM. For v2,
  swap to AWS KMS or a YubiHSM2 with a thin signer service.
