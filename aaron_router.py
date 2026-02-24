#!/usr/bin/env python3
"""
AARON Router — Edge-First Agentic Auth for Web4
================================================
Aaron is the authentication and routing layer for the OPTX network.
Handles Jett Auth (gaze biometrics) and Jett-Chat SSO (wallet signatures).

Endpoints:
  GET  /health           — Service health check
  POST /session          — Create auth session with QR challenge
  GET  /session/{id}     — Poll session status
  POST /verify           — Submit gaze proof for verification
  POST /gaze/analyze     — Classify iris landmarks into AGT regions

Deploy:
  pip install -r requirements.txt
  python aaron_router.py

Environment variables:
  AARON_PORT             — Port to listen on (default: 8888)
  SPACETIMEDB_URL        — SpacetimeDB instance URL (default: http://127.0.0.1:3000)
  SOLANA_RPC_URL         — Solana RPC endpoint (Helius recommended)
  ALLOWED_ORIGINS        — Comma-separated CORS origins

Learn more: https://astroknots.space/docs
"""

import asyncio
import hashlib
import json
import math
import os
import secrets
import time
import logging
from datetime import datetime
from typing import Optional
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ─── Config ───────────────────────────────────────────────────────────────────
SPACETIMEDB_URL = os.getenv("SPACETIMEDB_URL", "http://127.0.0.1:3000")
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.devnet.solana.com")
ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "https://jettoptics.ai,https://astroknots.space,http://localhost:3000",
).split(",")

SESSION_TTL = 120  # seconds (2 minutes)
MAX_SESSIONS = 1000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [AARON] %(message)s")
logger = logging.getLogger("aaron")


# ─── In-Memory Session Store ─────────────────────────────────────────────────
@dataclass
class AuthSession:
    session_id: str
    challenge: str
    wallet_address: Optional[str]
    origin: str
    created_at: float
    expires_at: float
    status: str  # pending | verified | expired
    verification_id: Optional[str] = None
    gaze_proof: Optional[dict] = None
    agt_weights: Optional[dict] = None  # {cog: float, emo: float, env: float}


sessions: dict[str, AuthSession] = {}


# ─── Pydantic Models ─────────────────────────────────────────────────────────
class SessionCreateRequest(BaseModel):
    """Request to create a new Jett Auth session."""
    wallet_address: Optional[str] = None
    origin: str = "https://jettoptics.ai"


class VerifyRequest(BaseModel):
    """Gaze proof submission from MOJO app after AGT calibration."""
    session_id: str
    challenge: str
    gaze_sequence: list[str]  # ["COG", "EMO", "ENV", "COG", "EMO", "ENV"]
    hold_durations: list[int]  # milliseconds per gaze position
    polynomial_encoding: str  # "132123" format (1=COG, 2=EMO, 3=ENV)
    verification_hash: str  # SHA-256 of gaze data
    wallet_address: Optional[str] = None


class GazeAnalyzeRequest(BaseModel):
    """Raw iris landmark data for real-time classification."""
    iris_landmarks: list[dict]  # [{x, y, z}] from MediaPipe FaceLandmarker
    face_landmarks: Optional[list[dict]] = None
    timestamp: float


# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="AARON Router",
    description="Edge-first agentic auth for Web4 — private compute, public proof",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Session Cleanup ─────────────────────────────────────────────────────────
async def cleanup_sessions():
    """Remove expired sessions periodically."""
    while True:
        now = time.time()
        expired = [sid for sid, s in sessions.items() if now > s.expires_at + 60]
        for sid in expired:
            del sessions[sid]
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired sessions")
        await asyncio.sleep(60)


@app.on_event("startup")
async def startup():
    asyncio.create_task(cleanup_sessions())
    logger.info(f"AARON Router started — port {os.getenv('AARON_PORT', 8888)}")


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "service": "aaron-router",
        "version": "1.0.0",
        "timestamp": datetime.utcnow().isoformat(),
        "active_sessions": len(sessions),
    }


@app.post("/session")
async def create_session(req: SessionCreateRequest):
    """
    Create a new Jett Auth session with QR challenge.

    Returns a session ID and challenge that gets encoded into a QR code.
    The MOJO app scans this QR and submits a gaze proof to /verify.
    """
    if len(sessions) >= MAX_SESSIONS:
        oldest = min(sessions.values(), key=lambda s: s.created_at)
        del sessions[oldest.session_id]

    session_id = secrets.token_urlsafe(24)
    challenge = secrets.token_hex(32)
    now = time.time()

    # Build QR payload for MOJO app
    verify_endpoint = f"{req.origin}/api/aaron/verify"
    qr_payload = json.dumps(
        {
            "protocol": "jett-auth-v1",
            "sessionId": session_id,
            "challenge": challenge,
            "expiresAt": int((now + SESSION_TTL) * 1000),
            "walletAddress": req.wallet_address,
            "endpoint": verify_endpoint,
            "steps": [
                "gaze_pin",
                "link_wallet",
                "stake_jtx",
                "genesis_sig",
                "camera_keys",
                "mint_optx",
            ],
        },
        separators=(",", ":"),
    )

    session = AuthSession(
        session_id=session_id,
        challenge=challenge,
        wallet_address=req.wallet_address,
        origin=req.origin,
        created_at=now,
        expires_at=now + SESSION_TTL,
        status="pending",
    )
    sessions[session_id] = session

    logger.info(f"Session created: {session_id[:12]}...")

    return {
        "sessionId": session_id,
        "challenge": challenge,
        "expiresAt": int((now + SESSION_TTL) * 1000),
        "qrPayload": qr_payload,
    }


@app.get("/session/{session_id}")
async def get_session(session_id: str):
    """Poll session status. Frontend calls this in a loop after showing QR."""
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    if time.time() > session.expires_at and session.status == "pending":
        session.status = "expired"

    result = {
        "sessionId": session_id,
        "status": session.status,
        "expiresAt": int(session.expires_at * 1000),
        "walletAddress": session.wallet_address,
    }

    if session.status == "verified":
        result["verificationId"] = session.verification_id
        result["agtWeights"] = session.agt_weights

    return result


@app.post("/verify")
async def verify_gaze(req: VerifyRequest):
    """
    Submit gaze proof for verification.

    The MOJO app calls this after the 6-step AGT calibration:
    1. gaze_pin — User gazes at calibration targets
    2. link_wallet — Phantom wallet connected
    3. stake_jtx — JTX stake verified
    4. genesis_sig — User signs genesis message
    5. camera_keys — Camera permissions granted
    6. mint_optx — Ready for OPTX minting

    Validates:
    - 4-6 gaze positions (COG/EMO/ENV only)
    - Each hold >= 500ms (prevents random taps)
    - Polynomial encoding matches sequence
    - Computes AGT weights and entropy score
    """
    session = sessions.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    if session.status != "pending":
        raise HTTPException(400, f"Session already {session.status}")

    if time.time() > session.expires_at:
        session.status = "expired"
        raise HTTPException(410, "Session expired")

    if req.challenge != session.challenge:
        raise HTTPException(403, "Challenge mismatch")

    # ─── Validate gaze proof ─────────────────────────────────────────────

    valid_tensors = {"COG", "EMO", "ENV"}
    if not (4 <= len(req.gaze_sequence) <= 6):
        raise HTTPException(400, "Gaze sequence must be 4-6 positions")
    if not all(t in valid_tensors for t in req.gaze_sequence):
        raise HTTPException(400, "Invalid tensor in gaze sequence")

    if len(req.hold_durations) != len(req.gaze_sequence):
        raise HTTPException(400, "Hold durations count mismatch")
    if any(d < 500 for d in req.hold_durations):
        raise HTTPException(400, "Each hold must be >= 500ms")

    # Polynomial encoding: 1=COG, 2=EMO, 3=ENV
    expected_encoding = "".join(
        "1" if t == "COG" else "2" if t == "EMO" else "3" for t in req.gaze_sequence
    )
    if req.polynomial_encoding != expected_encoding:
        raise HTTPException(400, "Polynomial encoding mismatch")

    # ─── Calculate AGT weights ────────────────────────────────────────────

    total = len(req.gaze_sequence)
    agt_weights = {
        "cog": round(req.gaze_sequence.count("COG") / total, 3),
        "emo": round(req.gaze_sequence.count("EMO") / total, 3),
        "env": round(req.gaze_sequence.count("ENV") / total, 3),
    }

    # Shannon entropy (higher = more varied = stronger auth)
    entropy = 0.0
    for w in agt_weights.values():
        if w > 0:
            entropy -= w * math.log2(w)
    entropy_score = int(entropy * 1000)  # Scale to match on-chain threshold (>= 750)

    logger.info(f"AGT weights: {agt_weights}, entropy: {entropy_score}")

    # ─── Update session ───────────────────────────────────────────────────

    verification_id = secrets.token_urlsafe(16)

    session.status = "verified"
    session.verification_id = verification_id
    session.gaze_proof = {
        "sequence": req.gaze_sequence,
        "holdDurations": req.hold_durations,
        "polynomialEncoding": req.polynomial_encoding,
        "entropyScore": entropy_score,
    }
    session.agt_weights = agt_weights
    if req.wallet_address:
        session.wallet_address = req.wallet_address

    logger.info(f"Session {req.session_id[:12]}... VERIFIED (entropy={entropy_score})")

    return {
        "status": "verified",
        "verificationId": verification_id,
        "walletAddress": session.wallet_address,
        "agtWeights": agt_weights,
        "entropyScore": entropy_score,
        "message": "Gaze proof accepted. Attestation stored.",
    }


@app.post("/gaze/analyze")
async def analyze_gaze(req: GazeAnalyzeRequest):
    """
    Classify raw iris landmarks into COG/EMO/ENV tensor regions.

    Used by MOJO app during real-time gaze capture to show the user
    which AGT region they're looking at.

    Region mapping:
    - COG (Cognitive, zone 1): Upper area (y < 0.4)
    - EMO (Emotional, zone 2): Bottom-left (y >= 0.4, x < 0.5)
    - ENV (Environmental, zone 3): Bottom-right (y >= 0.4, x >= 0.5)
    """
    if not req.iris_landmarks or len(req.iris_landmarks) < 4:
        raise HTTPException(400, "Need at least 4 iris landmarks")

    avg_x = sum(p.get("x", 0) for p in req.iris_landmarks) / len(req.iris_landmarks)
    avg_y = sum(p.get("y", 0) for p in req.iris_landmarks) / len(req.iris_landmarks)

    if avg_y < 0.4:
        tensor = "COG"
        confidence = min(1.0, (0.4 - avg_y) / 0.4)
    elif avg_x < 0.5:
        tensor = "EMO"
        confidence = min(1.0, (0.5 - avg_x) / 0.5 * (avg_y - 0.4) / 0.6)
    else:
        tensor = "ENV"
        confidence = min(1.0, (avg_x - 0.5) / 0.5 * (avg_y - 0.4) / 0.6)

    return {
        "tensor": tensor,
        "confidence": round(confidence, 3),
        "irisPosition": {"x": round(avg_x, 4), "y": round(avg_y, 4)},
        "timestamp": req.timestamp,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.getenv("AARON_PORT", "8888"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
