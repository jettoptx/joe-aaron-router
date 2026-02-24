/**
 * Aaron Client — TypeScript SDK for the OPTX Network
 * ====================================================
 * Light client for interacting with the Aaron Router.
 *
 * Install:
 *   npm install @astroknots/aaron  (coming soon)
 *   // or copy this file into your project
 *
 * Usage:
 *   import { AaronClient } from './aaron-client'
 *
 *   const aaron = new AaronClient('https://astroknots.space/optx')
 *   const session = await aaron.createSession({ walletAddress: 'your-pubkey' })
 *   // Show session.qrPayload as QR code
 *   // Poll session.sessionId for "verified" status
 *
 * Docs: https://astroknots.space/docs
 */

export interface AaronSession {
  sessionId: string
  challenge: string
  expiresAt: number
  qrPayload: string
}

export interface SessionStatus {
  sessionId: string
  status: "pending" | "verified" | "expired"
  expiresAt: number
  walletAddress: string | null
  verificationId?: string
  agtWeights?: AgtWeights
}

export interface AgtWeights {
  cog: number
  emo: number
  env: number
}

export interface VerifyResult {
  status: "verified"
  verificationId: string
  walletAddress: string | null
  agtWeights: AgtWeights
  entropyScore: number
  message: string
}

export interface GazeAnalysis {
  tensor: "COG" | "EMO" | "ENV"
  confidence: number
  irisPosition: { x: number; y: number }
  timestamp: number
}

export interface HealthStatus {
  status: string
  service: string
  version: string
  timestamp: string
  active_sessions: number
}

export class AaronClient {
  private baseUrl: string
  private timeout: number

  constructor(baseUrl = "https://astroknots.space/optx", timeout = 30000) {
    this.baseUrl = baseUrl.replace(/\/$/, "")
    this.timeout = timeout
  }

  private async request<T>(
    method: "GET" | "POST",
    path: string,
    body?: Record<string, unknown>
  ): Promise<T> {
    const url = `${this.baseUrl}${path}`
    const options: RequestInit = {
      method,
      headers: { "Content-Type": "application/json" },
      signal: AbortSignal.timeout(this.timeout),
    }
    if (body) {
      options.body = JSON.stringify(body)
    }
    const res = await fetch(url, options)
    if (!res.ok) {
      const text = await res.text()
      throw new Error(`Aaron ${method} ${path} failed (${res.status}): ${text}`)
    }
    return res.json()
  }

  /** Check Aaron Router health */
  async health(): Promise<HealthStatus> {
    return this.request("GET", "/health")
  }

  /** Create a new Jett Auth session */
  async createSession(opts?: {
    walletAddress?: string
    origin?: string
  }): Promise<AaronSession> {
    return this.request("POST", "/session", {
      wallet_address: opts?.walletAddress ?? null,
      origin: opts?.origin ?? "https://jettoptics.ai",
    })
  }

  /** Poll session status */
  async pollSession(sessionId: string): Promise<SessionStatus> {
    return this.request("GET", `/session/${sessionId}`)
  }

  /** Submit gaze proof for verification */
  async verify(proof: {
    sessionId: string
    challenge: string
    gazeSequence: string[]
    holdDurations: number[]
    polynomialEncoding: string
    verificationHash: string
    walletAddress?: string
  }): Promise<VerifyResult> {
    return this.request("POST", "/verify", {
      session_id: proof.sessionId,
      challenge: proof.challenge,
      gaze_sequence: proof.gazeSequence,
      hold_durations: proof.holdDurations,
      polynomial_encoding: proof.polynomialEncoding,
      verification_hash: proof.verificationHash,
      wallet_address: proof.walletAddress ?? null,
    })
  }

  /** Classify iris landmarks into AGT region */
  async analyzeGaze(
    irisLandmarks: Array<{ x: number; y: number; z?: number }>,
    timestamp: number
  ): Promise<GazeAnalysis> {
    return this.request("POST", "/gaze/analyze", {
      iris_landmarks: irisLandmarks,
      timestamp,
    })
  }

  /**
   * Wait for session to be verified (polling helper).
   * Resolves when status is "verified", rejects on "expired" or timeout.
   */
  async waitForVerification(
    sessionId: string,
    pollIntervalMs = 2000,
    timeoutMs = 120000
  ): Promise<SessionStatus> {
    const start = Date.now()
    while (Date.now() - start < timeoutMs) {
      const status = await this.pollSession(sessionId)
      if (status.status === "verified") return status
      if (status.status === "expired") throw new Error("Session expired")
      await new Promise((r) => setTimeout(r, pollIntervalMs))
    }
    throw new Error("Verification timed out")
  }
}
