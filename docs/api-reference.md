# Aaron Router — API Reference

Base URL: `https://astroknots.space/optx`

---

## GET /health

Health check.

**Response:**
```json
{
  "status": "healthy",
  "service": "aaron-router",
  "version": "1.0.0",
  "timestamp": "2026-02-24T14:00:00.000000",
  "active_sessions": 3
}
```

---

## POST /session

Create a new Jett Auth session. Returns a QR payload for the MOJO app.

**Request:**
```json
{
  "wallet_address": "EFvgELE1Hb4P...",  // optional Solana pubkey
  "origin": "https://jettoptics.ai"      // your domain
}
```

**Response:**
```json
{
  "sessionId": "DNC4vBIiT9C2-X-vs8Uy4AFh...",
  "challenge": "132194bc5b6b5e35eaedb6d10b6ac7d6...",
  "expiresAt": 1771970621153,
  "qrPayload": "{\"protocol\":\"jett-auth-v1\",...}"
}
```

The `qrPayload` is a JSON string. Render it as a QR code for the MOJO app to scan.

**Session expires in 2 minutes.** After that, `/session/{id}` returns `"expired"`.

---

## GET /session/{session_id}

Poll session status. Call this every 2 seconds after showing the QR code.

**Response (pending):**
```json
{
  "sessionId": "DNC4vBIi...",
  "status": "pending",
  "expiresAt": 1771970621153,
  "walletAddress": null
}
```

**Response (verified):**
```json
{
  "sessionId": "DNC4vBIi...",
  "status": "verified",
  "expiresAt": 1771970621153,
  "walletAddress": "EFvgELE1Hb4P...",
  "verificationId": "nkJ8znDx6QO3...",
  "agtWeights": { "cog": 0.333, "emo": 0.333, "env": 0.333 }
}
```

**Status values:** `pending` → `verified` | `expired`

---

## POST /verify

Submit a gaze proof from the MOJO app. This is called after the 6-step AGT calibration.

**Request:**
```json
{
  "session_id": "DNC4vBIi...",
  "challenge": "132194bc5b6b...",
  "gaze_sequence": ["COG", "ENV", "EMO", "COG", "EMO", "ENV"],
  "hold_durations": [650, 700, 550, 600, 680, 720],
  "polynomial_encoding": "132123",
  "verification_hash": "sha256-of-gaze-data",
  "wallet_address": "EFvgELE1Hb4P..."
}
```

**Validation rules:**
- `gaze_sequence`: 4-6 positions, only `"COG"`, `"EMO"`, `"ENV"`
- `hold_durations`: Same length as gaze_sequence, each >= 500ms
- `polynomial_encoding`: Must match sequence (1=COG, 2=EMO, 3=ENV)

**Response:**
```json
{
  "status": "verified",
  "verificationId": "nkJ8znDx6QO3...",
  "walletAddress": "EFvgELE1Hb4P...",
  "agtWeights": { "cog": 0.333, "emo": 0.333, "env": 0.333 },
  "entropyScore": 1584,
  "message": "Gaze proof accepted. Attestation stored."
}
```

**Entropy score:** Shannon entropy × 1000. On-chain threshold is 750. Max possible is 1585 (perfectly balanced COG/EMO/ENV).

---

## POST /gaze/analyze

Classify raw iris landmarks into AGT regions in real-time.

**Request:**
```json
{
  "iris_landmarks": [
    { "x": 0.48, "y": 0.32, "z": 0.01 },
    { "x": 0.52, "y": 0.31, "z": 0.01 },
    { "x": 0.50, "y": 0.35, "z": 0.01 },
    { "x": 0.49, "y": 0.33, "z": 0.01 }
  ],
  "timestamp": 1771970621.153
}
```

**Response:**
```json
{
  "tensor": "COG",
  "confidence": 0.85,
  "irisPosition": { "x": 0.4975, "y": 0.3275 },
  "timestamp": 1771970621.153
}
```

---

## Error Responses

All errors return:
```json
{
  "detail": "Error message here"
}
```

| Status | Meaning |
|--------|---------|
| 400 | Invalid request (bad gaze data, encoding mismatch, etc.) |
| 403 | Challenge mismatch |
| 404 | Session not found |
| 410 | Session expired |
