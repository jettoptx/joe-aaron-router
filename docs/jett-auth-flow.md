# Jett Auth — Gaze Biometric Authentication

## Overview

Jett Auth is a 6-step gaze-based authentication protocol. Users prove their identity by looking at specific screen regions, generating an **Augmented Gaze Tensor (AGT)** that becomes their cryptographic credential on Solana.

## The 6 Steps

| Step | Name | What Happens |
|------|------|-------------|
| 1 | `gaze_pin` | User gazes at calibration targets (4-6 positions) |
| 2 | `link_wallet` | Phantom wallet connected + pubkey captured |
| 3 | `stake_jtx` | JTX token stake verified (tier-gated access) |
| 4 | `genesis_sig` | User signs a genesis message with wallet |
| 5 | `camera_keys` | Camera permissions granted for iris tracking |
| 6 | `mint_optx` | OPTX token minted from accumulated entropy |

## AGT Regions

The screen is divided into three cognitive regions based on eye-tracking research:

```
┌─────────────────────────────┐
│                             │
│          COG (zone 1)       │  ← Upper area
│      Cognitive focus        │     y < 0.4
│                             │
├──────────────┬──────────────┤
│              │              │
│  EMO (zone 2)│ ENV (zone 3) │
│  Emotional   │ Environmental│  ← Lower area
│  processing  │ scanning     │     y >= 0.4
│  x < 0.5    │ x >= 0.5     │
└──────────────┴──────────────┘
```

## Polynomial Encoding

Each gaze sequence is encoded as a polynomial string:
- COG = 1, EMO = 2, ENV = 3
- Sequence `[COG, ENV, EMO, COG, EMO, ENV]` → encoding `"132123"`

This encoding is verified by the Aaron Router before accepting the proof.

## Entropy Scoring

**Shannon entropy** of the AGT weight distribution:

```
H = -Σ (w_i × log₂(w_i))
```

Scaled by 1000 for integer representation:
- **Minimum**: 0 (all gaze in one region — weak auth)
- **Threshold**: 750 (on-chain minimum for attestation)
- **Maximum**: 1585 (perfectly balanced COG/EMO/ENV — strongest auth)

## Integration Example

```typescript
// 1. Create session
const session = await aaron.createSession({ walletAddress: publicKey })

// 2. Display QR code
renderQR(session.qrPayload)

// 3. Wait for MOJO app to complete gaze calibration
const verified = await aaron.waitForVerification(session.sessionId)

// 4. User is authenticated
console.log(verified.agtWeights)    // { cog: 0.33, emo: 0.33, env: 0.33 }
console.log(verified.entropyScore)  // 1584
```
