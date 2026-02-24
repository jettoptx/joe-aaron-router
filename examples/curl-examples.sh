#!/bin/bash
# Aaron Router — curl examples
# Base URL: https://astroknots.space/optx (or http://localhost:8888 for local dev)

BASE="https://astroknots.space/optx"

echo "=== Health Check ==="
curl -s "$BASE/health" | python3 -m json.tool

echo ""
echo "=== Create Session ==="
SESSION=$(curl -s "$BASE/session" \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{"wallet_address": null, "origin": "https://jettoptics.ai"}')
echo "$SESSION" | python3 -m json.tool

# Extract session ID and challenge
SID=$(echo "$SESSION" | python3 -c "import sys,json; print(json.load(sys.stdin)['sessionId'])")
CHAL=$(echo "$SESSION" | python3 -c "import sys,json; print(json.load(sys.stdin)['challenge'])")
echo ""
echo "Session ID: $SID"
echo "Challenge:  $CHAL"

echo ""
echo "=== Poll Session (should be pending) ==="
curl -s "$BASE/session/$SID" | python3 -m json.tool

echo ""
echo "=== Submit Gaze Proof ==="
curl -s "$BASE/verify" \
  -X POST \
  -H "Content-Type: application/json" \
  -d "{
    \"session_id\": \"$SID\",
    \"challenge\": \"$CHAL\",
    \"gaze_sequence\": [\"COG\", \"ENV\", \"EMO\", \"COG\", \"EMO\", \"ENV\"],
    \"hold_durations\": [650, 700, 550, 600, 680, 720],
    \"polynomial_encoding\": \"132123\",
    \"verification_hash\": \"test\",
    \"wallet_address\": null
  }" | python3 -m json.tool

echo ""
echo "=== Poll Session (should be verified) ==="
curl -s "$BASE/session/$SID" | python3 -m json.tool

echo ""
echo "=== Analyze Gaze (COG region) ==="
curl -s "$BASE/gaze/analyze" \
  -X POST \
  -H "Content-Type: application/json" \
  -d '{
    "iris_landmarks": [
      {"x": 0.48, "y": 0.32, "z": 0.01},
      {"x": 0.52, "y": 0.31, "z": 0.01},
      {"x": 0.50, "y": 0.35, "z": 0.01},
      {"x": 0.49, "y": 0.33, "z": 0.01}
    ],
    "timestamp": 1234567890.0
  }' | python3 -m json.tool
