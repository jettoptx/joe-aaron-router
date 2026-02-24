# Aaron Router

> Edge-first agentic auth for Web4 — private compute, public proof

Aaron is the authentication and routing layer for the **OPTX network**. It handles:

- **Jett Auth** — Gaze biometric authentication via AGT (Augmented Gaze Tensor)
- **Jett-Chat SSO** — Wallet signature authentication for real-time chat
- **x402 Payments** — Micropayments for domain registration and NFT minting

## Quick Start

```bash
pip install -r requirements.txt
python aaron_router.py
```

Aaron starts on port 8888 by default. Override with `AARON_PORT` env var.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Service health check |
| `POST` | `/session` | Create auth session (returns QR payload) |
| `GET` | `/session/{id}` | Poll session status |
| `POST` | `/verify` | Submit gaze proof |
| `POST` | `/gaze/analyze` | Classify iris landmarks into AGT regions |

## SDK

### Python

```python
from sdk.python.aaron_client import AaronClient

client = AaronClient("https://astroknots.space/optx")
session = client.create_session(wallet_address="your-solana-pubkey")
# Show session["qrPayload"] as QR code
# MOJO app scans QR → submits gaze proof → session becomes "verified"
status = client.poll_session(session["sessionId"])
```

### TypeScript

```typescript
import { AaronClient } from './sdk/typescript/aaron-client'

const aaron = new AaronClient('https://astroknots.space/optx')
const session = await aaron.createSession({ walletAddress: 'your-pubkey' })
// Show session.qrPayload as QR code
const result = await aaron.waitForVerification(session.sessionId)
console.log(result.agtWeights) // { cog: 0.33, emo: 0.33, env: 0.33 }
```

## Auth Flow

```
┌─────────┐     ┌──────────────┐     ┌───────────┐     ┌──────────┐
│ Frontend │────>│ Aaron Router │────>│ SpacetimeDB│────>│  Solana  │
│ (Next.js)│<────│ (Jetson Edge)│<────│  (Edge DB) │<────│ (Devnet) │
└─────────┘     └──────────────┘     └───────────┘     └──────────┘
     │                  │
     │  1. POST /session│
     │  2. Show QR      │
     │                  │
     │    MOJO App      │
     │  3. Scan QR ────>│
     │  4. Gaze capture │
     │  5. POST /verify │
     │                  │
     │  6. Poll status  │
     │  7. "verified" ──│──> Attestation on Solana
     │                  │
```

## AGT Regions

The Augmented Gaze Tensor maps eye gaze to three cognitive regions:

| Region | Zone | Description |
|--------|------|-------------|
| **COG** | 1 (upper) | Cognitive focus — analytical attention |
| **EMO** | 2 (lower-left) | Emotional processing — empathetic awareness |
| **ENV** | 3 (lower-right) | Environmental scanning — spatial awareness |

Entropy is calculated via Shannon entropy of the AGT weights. Higher entropy (more varied gaze pattern) = stronger authentication. On-chain threshold: **750+**.

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AARON_PORT` | `8888` | Server port |
| `SPACETIMEDB_URL` | `http://127.0.0.1:3000` | SpacetimeDB instance |
| `SOLANA_RPC_URL` | `https://api.devnet.solana.com` | Solana RPC (use Helius for prod) |
| `ALLOWED_ORIGINS` | `https://jettoptics.ai,...` | CORS origins (comma-separated) |

## On-Chain Addresses

| Token | Network | Address |
|-------|---------|---------|
| $OPTX | Devnet | `4r9WxVWBNMphYfSyGBuMFYRLsLEnzUNquJPnpFessXRH` |
| $JTX | Mainnet | `9XpJiKEYzq5yDo5pJzRfjSRMPL2yPfDQXgiN7uYtBhUj` |
| $CSTB | Devnet | `4waAimBGeubfVBp4MX9vRh7iTWxoR2RYYqiuChqCH7rX` |
| DePIN Program | Devnet | `91SqPNGRFrTgwSM3S7grZK8A6TCqn5STFGK4mAfqWMbQ` |

## License

MIT — Built by [Jett Optics](https://jettoptics.ai)
