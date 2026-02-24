/**
 * Next.js API route: /api/aaron/session
 *
 * Proxies session creation to Aaron Router.
 * Set AARON_ROUTER_URL in your Vercel env vars.
 */
import { NextRequest, NextResponse } from "next/server"

const AARON_URL = process.env.AARON_ROUTER_URL || "http://localhost:8888"

export async function POST(req: NextRequest) {
  try {
    const body = await req.json()
    const res = await fetch(`${AARON_URL}/session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10_000),
    })
    const data = await res.json()
    return NextResponse.json(data, { status: res.status })
  } catch {
    return NextResponse.json(
      { error: "Aaron Router unreachable" },
      { status: 502 }
    )
  }
}

export async function GET(req: NextRequest) {
  const sessionId = req.nextUrl.searchParams.get("sessionId")
  if (!sessionId) {
    return NextResponse.json({ error: "Missing sessionId" }, { status: 400 })
  }
  try {
    const res = await fetch(`${AARON_URL}/session/${sessionId}`, {
      signal: AbortSignal.timeout(5_000),
    })
    const data = await res.json()
    return NextResponse.json(data, { status: res.status })
  } catch {
    return NextResponse.json(
      { error: "Aaron Router unreachable" },
      { status: 502 }
    )
  }
}
