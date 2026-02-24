"""
Aaron Client — Python SDK for the OPTX Network
================================================
Light client for interacting with the Aaron Router.
No internal AGT logic — just HTTP calls to the router.

Install:
    pip install requests

Usage:
    from aaron_client import AaronClient

    client = AaronClient("https://astroknots.space/optx")
    session = client.create_session(wallet_address="your-solana-pubkey")
    print(session["qrPayload"])  # Show as QR code

    # After MOJO app submits gaze proof:
    status = client.poll_session(session["sessionId"])
    print(status["status"])  # "verified"

Docs: https://astroknots.space/docs
"""

import requests
from typing import Dict, Any, Optional, List


class AaronClient:
    """Client for the Aaron Router — OPTX network authentication."""

    def __init__(self, base_url: str = "https://astroknots.space/optx"):
        self.base_url = base_url.rstrip("/")
        self.timeout = 30

    def health(self) -> Dict[str, Any]:
        """Check Aaron Router health."""
        resp = requests.get(f"{self.base_url}/health", timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def create_session(
        self,
        wallet_address: Optional[str] = None,
        origin: str = "https://jettoptics.ai",
    ) -> Dict[str, Any]:
        """
        Create a new Jett Auth session.

        Returns:
            {
                "sessionId": "abc123...",
                "challenge": "hex-challenge...",
                "expiresAt": 1234567890000,
                "qrPayload": "{...}"  # JSON string for QR code
            }
        """
        payload = {"wallet_address": wallet_address, "origin": origin}
        resp = requests.post(
            f"{self.base_url}/session", json=payload, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def poll_session(self, session_id: str) -> Dict[str, Any]:
        """
        Poll session status.

        Returns:
            {
                "sessionId": "...",
                "status": "pending" | "verified" | "expired",
                "agtWeights": {"cog": 0.33, "emo": 0.33, "env": 0.33},  # if verified
                "verificationId": "..."  # if verified
            }
        """
        resp = requests.get(
            f"{self.base_url}/session/{session_id}", timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def verify(
        self,
        session_id: str,
        challenge: str,
        gaze_sequence: List[str],
        hold_durations: List[int],
        polynomial_encoding: str,
        verification_hash: str,
        wallet_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Submit gaze proof for verification.

        Args:
            session_id: From create_session response
            challenge: From create_session response
            gaze_sequence: ["COG", "EMO", "ENV", "COG", "EMO", "ENV"]
            hold_durations: [650, 700, 550, 600, 680, 720] (ms per position)
            polynomial_encoding: "132123" (1=COG, 2=EMO, 3=ENV)
            verification_hash: SHA-256 of gaze data
            wallet_address: Solana wallet pubkey

        Returns:
            {
                "status": "verified",
                "verificationId": "...",
                "agtWeights": {"cog": 0.33, "emo": 0.33, "env": 0.33},
                "entropyScore": 1584,
                "message": "Gaze proof accepted."
            }
        """
        payload = {
            "session_id": session_id,
            "challenge": challenge,
            "gaze_sequence": gaze_sequence,
            "hold_durations": hold_durations,
            "polynomial_encoding": polynomial_encoding,
            "verification_hash": verification_hash,
            "wallet_address": wallet_address,
        }
        resp = requests.post(
            f"{self.base_url}/verify", json=payload, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()

    def analyze_gaze(
        self,
        iris_landmarks: List[Dict[str, float]],
        timestamp: float,
    ) -> Dict[str, Any]:
        """
        Classify iris landmarks into AGT region (COG/EMO/ENV).

        Args:
            iris_landmarks: [{x, y, z}, ...] from MediaPipe FaceLandmarker
            timestamp: Unix timestamp

        Returns:
            {
                "tensor": "COG" | "EMO" | "ENV",
                "confidence": 0.85,
                "irisPosition": {"x": 0.5, "y": 0.3}
            }
        """
        payload = {"iris_landmarks": iris_landmarks, "timestamp": timestamp}
        resp = requests.post(
            f"{self.base_url}/gaze/analyze", json=payload, timeout=self.timeout
        )
        resp.raise_for_status()
        return resp.json()


# ─── Convenience usage ────────────────────────────────────────────────────────
if __name__ == "__main__":
    client = AaronClient()
    print("Health:", client.health())
