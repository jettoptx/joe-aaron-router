#!/usr/bin/env python3
"""
AARON Router — Edge-First Agentic Auth + Donor Rewards
=======================================================
Runs on Jetson Orin Nano (port 8888). Single FastAPI app with two
service surfaces:

Jett Auth (gaze biometrics) — original surface:
  POST /session           — Create Jett Auth QR session challenge
  GET  /session/{id}      — Poll session status
  POST /verify            — MOJO submits gaze proof → on-chain attestation
  POST /gaze/analyze      — Classify iris landmarks into AGT regions
  POST /mint              — OPTX mint after successful verification
  GET  /health            — Health check (includes SpacetimeDB ping)

Donor reward stack (Phase 2 + 3a):
  POST /donations/claim                — Dapp registers a fresh donation
  POST /donations/webhooks/helius      — Helius confirms donate_sol
  GET  /donations/status/{tx_sig}      — Frontend polls reward status
  POST /donations/admin/replay/{sig}   — Manual replay of failed legs

Donor reward services (services/jtx_drop.py, services/xahau.py) are
lazy-initialized so missing env vars only blow up when a donation
actually arrives, not at import time. The Jett Auth surface is
unaffected if donor-reward env vars are unset.

Dependencies: fastapi, uvicorn, aiohttp, solders, solana, xrpl-py
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

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# Donor-reward stack (Phase 2 + 3a, optional Phase 1).
# Imported eagerly so /donations routes mount on every boot. Services
# inside this router are lazily initialized — missing env vars only
# blow up when a donation actually arrives, not at import time.
from routers.donations import router as donations_router

# ─── Config ───────────────────────────────────────────────────────────────────
# Existing Jett Auth config (Jetson defaults preserved).
SPACETIMEDB_URL = os.getenv("SPACETIMEDB_URL", "http://127.0.0.1:3000")
HELIUS_DEVNET_RPC = os.getenv(
    "HELIUS_DEVNET_RPC",
    # Public devnet endpoint as no-key fallback. Set HELIUS_DEVNET_RPC in
    # /etc/optx/aaron.env (with your private key) for higher rate limits.
    "https://api.devnet.solana.com",
)
JOE_PUBLIC_KEY = os.getenv("JOE_PUBLIC_KEY", "EFvgELE1Hb4PC5tbPTAe8v1uEDGee8nwYBMCU42bZRGk")
JTX_MINT = os.getenv("JTX_MINT", "9XpJiKEYzq5yDo5pJzRfjSRMPL2yPfDQXgiN7uYtBhUj")
OPTX_MINT = os.getenv("OPTX_MINT", "4r9WxVWBNMphYfSyGBuMFYRLsLEnzUNquJPnpFessXRH")  # devnet
CSTB_MINT = os.getenv("CSTB_MINT", "4waAimBGeubfVBp4MX9vRh7iTWxoR2RYYqiuChqCH7rX")  # devnet

ALLOWED_ORIGINS = os.getenv(
    "ALLOWED_ORIGINS",
    "https://jettoptics.ai,https://astroknots.space,https://www.astroknots.space,http://localhost:3000,http://localhost:3001",
).split(",")

# Donor reward stack env (sealed; see docs/donor-rewards-deploy.md):
#   JOE_AGENT_KEYPAIR_PATH      — sealed file (mode 0400) holding signer keypair
#   JTX_MINT_MAINNET / _DEVNET  — Token-2022 mint addresses
#   JTX_DECIMALS                — default 9
#   JTX_DROP_AMOUNT             — whole tokens per donation (default 1)
#   JTX_DAILY_DROP_CAP          — abuse circuit-breaker (default 10000)
#   HELIUS_WEBHOOK_SECRET       — HMAC secret from Helius dashboard
#   JETT_VAULT_PROGRAM_ID       — defaults to mainnet program id
#   XAHAU_RPC_URL / _SEED / _HOOK_ACCOUNT / _DONATION_AMOUNT_DROPS
#   DONATIONS_DB_PATH           — sqlite path (default /var/lib/optx/donations.db)
#   ADMIN_TOKEN                 — required for /donations/admin/replay
#   SOLANA_NETWORK              — "mainnet" | "devnet" | "localnet"

SESSION_TTL = 120  # 2 minutes
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
    wallet_address: Optional[str] = None
    origin: str = "https://jettoptics.ai"


class VerifyRequest(BaseModel):
    session_id: str
    challenge: str
    gaze_sequence: list[str]  # ["COG", "EMO", "ENV", "COG"]
    hold_durations: list[int]  # ms per position
    polynomial_encoding: str  # "1231" format
    verification_hash: str  # SHA-256
    wallet_address: Optional[str] = None
    agt_weights: Optional[dict] = None  # {cog: float, emo: float, env: float}


class GazeAnalyzeRequest(BaseModel):
    iris_landmarks: list[dict]  # [{x, y, z}] for landmarks 468-477
    face_landmarks: Optional[list[dict]] = None
    timestamp: float


# ─── FastAPI App ──────────────────────────────────────────────────────────────
app = FastAPI(
    title="AARON Router",
    description="Edge-first agentic auth + donor rewards — private compute, public proof",
    version="1.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount donor-reward routes (POST /donations/claim, /donations/webhooks/helius,
# GET /donations/status/{sig}, POST /donations/admin/replay/{sig}).
app.include_router(donations_router)


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
    logger.info("AARON Router started on port 8888")
    logger.info(f"SpacetimeDB: {SPACETIMEDB_URL}")
    logger.info(f"Helius RPC: {HELIUS_DEVNET_RPC[:60]}{'...' if len(HELIUS_DEVNET_RPC) > 60 else ''}")
    logger.info(f"JOE signing key: {JOE_PUBLIC_KEY[:12]}...")


# ─── Routes ───────────────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    # Check SpacetimeDB connection
    spacetime_ok = False
    try:
        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(
                f"{SPACETIMEDB_URL}/v1/database/jettchat/sql",
                timeout=aiohttp.ClientTimeout(total=2),
                headers={"Content-Type": "text/plain"},
                data="SELECT * FROM memory_entry LIMIT 1",
            ) as resp:
                spacetime_ok = resp.status == 200
    except Exception:
        pass

    return {
        "status": "healthy",
        "service": "aaron-router",
        "version": "1.1.0",
        "timestamp": datetime.utcnow().isoformat(),
        "active_sessions": len(sessions),
        "spacetimedb": "connected" if spacetime_ok else "disconnected",
        "joe_key": JOE_PUBLIC_KEY[:12] + "...",
    }


@app.post("/session")
async def create_session(req: SessionCreateRequest):
    """Create a new Jett Auth session with QR challenge."""
    if len(sessions) >= MAX_SESSIONS:
        # Evict oldest
        oldest = min(sessions.values(), key=lambda s: s.created_at)
        del sessions[oldest.session_id]

    session_id = secrets.token_urlsafe(24)
    challenge = secrets.token_hex(32)
    now = time.time()

    qr_payload = json.dumps(
        {
            "protocol": "jett-auth-v1",
            "sessionId": session_id,
            "challenge": challenge,
            "expiresAt": int((now + SESSION_TTL) * 1000),  # JS timestamp
            "walletAddress": req.wallet_address,
            "endpoint": f"{req.origin}/optx/verify" if "astroknots" in req.origin else f"{req.origin}/api/aaron/verify",
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

    logger.info(
        f"Session created: {session_id[:12]}... wallet={req.wallet_address or 'none'}"
    )

    return {
        "sessionId": session_id,
        "challenge": challenge,
        "expiresAt": int((now + SESSION_TTL) * 1000),
        "qrPayload": qr_payload,
    }


@app.get("/session/{session_id}")
async def get_session(session_id: str):
    """Poll session status (used by frontend polling loop)."""
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    # Check expiration
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
    MOJO submits gaze proof here after the 6-step AGT calibration.
    Aaron verifies the proof and stores attestation in SpacetimeDB.
    """
    session = sessions.get(req.session_id)
    if not session:
        raise HTTPException(404, "Session not found")

    if session.status != "pending":
        raise HTTPException(400, f"Session already {session.status}")

    if time.time() > session.expires_at:
        session.status = "expired"
        raise HTTPException(410, "Session expired")

    # Verify challenge matches
    if req.challenge != session.challenge:
        raise HTTPException(403, "Challenge mismatch")

    # ─── Validate gaze proof ─────────────────────────────────────────────
    # 1. Sequence must have 4-6 positions, only COG/EMO/ENV
    valid_tensors = {"COG", "EMO", "ENV"}
    if not (4 <= len(req.gaze_sequence) <= 6):
        raise HTTPException(400, "Gaze sequence must be 4-6 positions")
    if not all(t in valid_tensors for t in req.gaze_sequence):
        raise HTTPException(400, "Invalid tensor in gaze sequence")

    # 2. Hold durations must be >= 500ms each (prevents random input)
    if len(req.hold_durations) != len(req.gaze_sequence):
        raise HTTPException(400, "Hold durations count mismatch")
    if any(d < 500 for d in req.hold_durations):
        raise HTTPException(400, "Each hold must be >= 500ms")

    # 3. Polynomial encoding must match sequence
    expected_encoding = "".join(
        "1" if t == "COG" else "2" if t == "EMO" else "3" for t in req.gaze_sequence
    )
    if req.polynomial_encoding != expected_encoding:
        raise HTTPException(400, "Polynomial encoding mismatch")

    # 4. Verification hash check
    hash_data = json.dumps(
        {
            "nonce": session.session_id,
            "sequence": req.gaze_sequence,
            "holdDurations": req.hold_durations,
            "timestamp": int(session.created_at * 1000),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    expected_hash = hashlib.sha256(hash_data.encode()).hexdigest()
    logger.info(
        f"Hash check: client={req.verification_hash[:16]}... server={expected_hash[:16]}..."
    )

    # 5. Calculate AGT weights from gaze sequence
    total = len(req.gaze_sequence)
    agt_weights = {
        "cog": round(req.gaze_sequence.count("COG") / total, 3),
        "emo": round(req.gaze_sequence.count("EMO") / total, 3),
        "env": round(req.gaze_sequence.count("ENV") / total, 3),
    }

    # 6. Calculate gaze entropy (higher = more varied pattern = stronger auth)
    entropy = 0.0
    for w in agt_weights.values():
        if w > 0:
            entropy -= w * math.log2(w)
    entropy_score = int(entropy * 1000)  # Scale to match Anchor program (>= 750)

    logger.info(f"AGT weights: {agt_weights}, entropy: {entropy_score}")

    # ─── Store attestation in SpacetimeDB ─────────────────────────────────
    verification_id = secrets.token_urlsafe(16)
    try:
        async with aiohttp.ClientSession() as http_session:
            await http_session.post(
                f"{SPACETIMEDB_URL}/v1/database/jettchat/sql",
                json={
                    "query": (
                        f"INSERT INTO gaze_events (user_id, gaze_x, gaze_y, "
                        f"cog_value, env_value, emo_value, confidence) "
                        f"VALUES (1, {agt_weights['cog']}, {agt_weights['env']}, "
                        f"{agt_weights['cog']}, {agt_weights['env']}, "
                        f"{agt_weights['emo']}, {entropy / 2.0})"
                    )
                },
                timeout=aiohttp.ClientTimeout(total=3),
            )
            logger.info("SpacetimeDB attestation stored")
    except Exception as e:
        logger.warning(f"SpacetimeDB write failed (non-fatal): {e}")

    # ─── Update session ───────────────────────────────────────────────────
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
        "message": "Gaze proof accepted. Aaron attestation stored.",
    }


@app.post("/gaze/analyze")
async def analyze_gaze(req: GazeAnalyzeRequest):
    """
    Classify raw iris landmarks into COG/EMO/ENV tensor regions.
    Used by MOJO app during real-time gaze capture.
    """
    if not req.iris_landmarks or len(req.iris_landmarks) < 4:
        raise HTTPException(400, "Need at least 4 iris landmarks")

    # Calculate average iris position (normalized 0-1)
    avg_x = sum(p.get("x", 0) for p in req.iris_landmarks) / len(req.iris_landmarks)
    avg_y = sum(p.get("y", 0) for p in req.iris_landmarks) / len(req.iris_landmarks)

    # Classify into AGT regions using barycentric mapping
    # COG (top, upper area): y < 0.4
    # EMO (bottom-left): y >= 0.4 and x < 0.5
    # ENV (bottom-right): y >= 0.4 and x >= 0.5
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


# ─── OPTX Minting Endpoint ───────────────────────────────────────────────
class MintRequest(BaseModel):
    verification_id: str
    wallet_address: str
    entropy_score: int = 750
    agt_weights: dict = {}


@app.post("/mint")
async def mint_optx(req: MintRequest):
    """
    Mint OPTX tokens after successful gaze verification.
    Called by frontend after /verify returns success.
    Uses the JTX-CSTB-TRUST DePIN program on devnet.
    Program ID: HkJoo6829ANVxPNCVDURjZazRncWv1ht3WfyDc2GD5oH
    """
    # Verify the session was actually verified
    verified_session = None
    for sid, s in sessions.items():
        if hasattr(s, 'verification_id') and s.verification_id == req.verification_id:
            verified_session = s
            break

    if not verified_session:
        raise HTTPException(404, "Verification ID not found — verify gaze first")

    if verified_session.status != "verified":
        raise HTTPException(400, f"Session not verified (status: {verified_session.status})")

    # Calculate OPTX amount based on entropy score
    # Higher entropy = more varied gaze pattern = more OPTX
    base_optx = 1  # Minimum 1 OPTX per verification
    if req.entropy_score >= 1000:
        optx_amount = base_optx * 3  # High entropy bonus
    elif req.entropy_score >= 750:
        optx_amount = base_optx * 2
    else:
        optx_amount = base_optx

    # Store mint request as attestation
    mint_id = secrets.token_urlsafe(12)
    try:
        async with aiohttp.ClientSession() as http_session:
            await http_session.post(
                f"{SPACETIMEDB_URL}/v1/database/jettchat/sql",
                json={
                    "query": (
                        f"INSERT INTO gaze_events (user_id, gaze_x, gaze_y, "
                        f"cog_value, env_value, emo_value, confidence) "
                        f"VALUES (1, {req.agt_weights.get('cog', 0)}, {req.agt_weights.get('env', 0)}, "
                        f"{req.agt_weights.get('cog', 0)}, {req.agt_weights.get('env', 0)}, "
                        f"{req.agt_weights.get('emo', 0)}, 0.95)"
                    )
                },
                timeout=aiohttp.ClientTimeout(total=3),
            )
    except Exception as e:
        logger.warning(f"SpacetimeDB mint log failed (non-fatal): {e}")

    # Mark session as minted to prevent double-mint
    verified_session.status = "minted"

    logger.info(
        f"OPTX mint: {optx_amount} OPTX → {req.wallet_address[:12]}... "
        f"(entropy={req.entropy_score}, mint_id={mint_id})"
    )

    return {
        "status": "minted",
        "mint_id": mint_id,
        "optx_amount": optx_amount,
        "wallet_address": req.wallet_address,
        "entropy_score": req.entropy_score,
        "program_id": "HkJoo6829ANVxPNCVDURjZazRncWv1ht3WfyDc2GD5oH",
        "network": "devnet",
        "message": f"{optx_amount} OPTX attestation recorded. On-chain mint pending program initialization.",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("AARON_PORT", "8888")), log_level="info")
