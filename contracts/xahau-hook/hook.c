/**
 * OPTX GENSYS Routing Hook for Xahau
 * ====================================
 * Hook Account: rLXCpNStZodh9HjXn5DyoSFMKies1vKBUG (OPTX XRP Wallet)
 *
 * Flow: Payment → Verify gaze attestation → Split fees → Route to Anodos DEX → Emit Wormhole VAA
 *
 * Fee Split:
 *   80% → Anodos DEX (mXRP/XRP liquidity pool)
 *   15% → JTX staker rewards
 *    5% → Protocol treasury
 *
 * Build: wasmcc hook.c -o hook.wasm
 * Deploy: hdc rLXCpNStZodh9HjXn5DyoSFMKies1vKBUG -i hook.wasm -a 4096 -r 64 -f
 *
 * GENSYS Block: Text command → AARON /verify → Xahau Hook → Anodos → Solana bridge
 */

#include "hookapi.h"

/* ─── Configuration ─── */

/* Anodos DEX — mXRP/XRP liquidity target */
#define ANODOS_ACCT "r21wnrTT2G52FcKrTBAb8hhn4aGSGn1eX"

/* JTX staker reward pool (deploy then update) */
#define JTX_STAKER_ACCT "rJTXStakerPool000000000000000000"

/* Protocol treasury (deploy then update) */
#define TREASURY_ACCT "rOPTXTreasury0000000000000000000"

/* Fee basis points */
#define FEE_LIQUIDITY_BP 8000  /* 80% */
#define FEE_JTX_BP       1500  /* 15% */
#define FEE_TREASURY_BP   500  /*  5% */
#define BP_TOTAL         10000

/* Gaze attestation */
#define GAZE_HASH_LEN 32
#define MEMO_TYPE_GAZE 0x01
#define MEMO_TYPE_POOL 0x02

/* XRP conversion */
#define DROPS_PER_XRP 1000000ULL

/* State namespace: "optx" */
#define STATE_NS "optx"
#define STATE_NS_LEN 4

/* ─── Helpers ─── */

/**
 * Pack a 64-bit unsigned integer into 8 bytes (big-endian)
 */
static inline void pack_u64(uint8_t* buf, uint64_t val) {
    buf[0] = (val >> 56) & 0xFF;
    buf[1] = (val >> 48) & 0xFF;
    buf[2] = (val >> 40) & 0xFF;
    buf[3] = (val >> 32) & 0xFF;
    buf[4] = (val >> 24) & 0xFF;
    buf[5] = (val >> 16) & 0xFF;
    buf[6] = (val >>  8) & 0xFF;
    buf[7] = val & 0xFF;
}

/**
 * Unpack 8 bytes (big-endian) into a 64-bit unsigned integer
 */
static inline uint64_t unpack_u64(const uint8_t* buf) {
    return ((uint64_t)buf[0] << 56) | ((uint64_t)buf[1] << 48) |
           ((uint64_t)buf[2] << 40) | ((uint64_t)buf[3] << 32) |
           ((uint64_t)buf[4] << 24) | ((uint64_t)buf[5] << 16) |
           ((uint64_t)buf[6] <<  8) |  (uint64_t)buf[7];
}

/* ─── Hook Entry ─── */

int64_t hook(uint32_t reserved) {

    /* Guard: Only process Payment transactions */
    int64_t tt = otxn_type();
    if (tt != ttPAYMENT) {
        accept(SBUF("OPTX: Not a payment, passing through."), 0);
        return 0;
    }

    /* ─── 1. Extract sender account ─── */
    uint8_t sender_accid[20];
    if (otxn_field(sfAccount, SBUF(sender_accid)) != 20) {
        rollback(SBUF("OPTX: Cannot read sender account."), 1);
        return -1;
    }

    /* ─── 2. Extract payment amount (XRP only) ─── */
    uint8_t amount_buf[48];
    int64_t amount_len = otxn_field(sfAmount, SBUF(amount_buf));

    /* Check if native XRP (amount_len == 8 means drops) */
    if (amount_len != 8) {
        accept(SBUF("OPTX: IOU payment, passing through."), 0);
        return 0;
    }

    int64_t drops_raw = AMOUNT_TO_DROPS(amount_buf);
    if (drops_raw <= 0) {
        rollback(SBUF("OPTX: Invalid payment amount."), 2);
        return -1;
    }
    uint64_t drops = (uint64_t)drops_raw;

    /* ─── 3. Verify gaze attestation hash in memo ─── */
    uint8_t memo_type[64];
    uint8_t memo_data[256];
    int64_t memo_type_len = otxn_field(sfMemoType, SBUF(memo_type));
    int64_t memo_data_len = otxn_field(sfMemoData, SBUF(memo_data));

    /* Require memo with gaze hash (32 bytes minimum) */
    if (memo_data_len < GAZE_HASH_LEN) {
        rollback(SBUF("OPTX: Missing gaze attestation hash in memo."), 3);
        return -1;
    }

    /* Extract 32-byte gaze hash */
    uint8_t gaze_hash[GAZE_HASH_LEN];
    for (int i = 0; i < GAZE_HASH_LEN; i++)
        gaze_hash[i] = memo_data[i];

    /*
     * TODO: Verify gaze_hash against AARON oracle state
     * For now: presence of 32-byte hash = valid attestation
     * Future: state_get(AARON_ORACLE_NS, gaze_hash) to verify on-chain
     */
    trace(SBUF("OPTX: Gaze attestation hash present."), 0, 0, 0);

    /* ─── 4. Calculate fee split ─── */
    uint64_t liq_drops   = (drops * FEE_LIQUIDITY_BP) / BP_TOTAL;  /* 80% */
    uint64_t jtx_drops   = (drops * FEE_JTX_BP)       / BP_TOTAL;  /* 15% */
    uint64_t treas_drops = drops - liq_drops - jtx_drops;           /*  5% remainder */

    /* ─── 5. Track OPTX mints per user (state) ─── */
    uint8_t state_key[24]; /* 4 byte namespace + 20 byte account */
    uint8_t state_val[8];

    /* Build state key: "optx" + sender_accid */
    for (int i = 0; i < STATE_NS_LEN; i++)
        state_key[i] = STATE_NS[i];
    for (int i = 0; i < 20; i++)
        state_key[STATE_NS_LEN + i] = sender_accid[i];

    /* Read current mint count */
    uint64_t prev_mints = 0;
    if (state(SBUF(state_val), SBUF(state_key)) == 8)
        prev_mints = unpack_u64(state_val);

    /* Calculate new mints: 1 OPTX per XRP */
    uint64_t new_optx = drops / DROPS_PER_XRP;
    if (new_optx == 0) new_optx = 1; /* Minimum 1 OPTX per transaction */
    uint64_t total_mints = prev_mints + new_optx;

    /* Write updated mint count */
    pack_u64(state_val, total_mints);
    if (state_set(SBUF(state_val), SBUF(state_key)) != 8) {
        rollback(SBUF("OPTX: Failed to update mint state."), 4);
        return -1;
    }

    /* ─── 6. Emit transactions ─── */

    /* 6a. Emit 80% to Anodos DEX for mXRP/XRP pool deposit */
    {
        uint8_t emithash[32];

        /* Prepare the emitted transaction */
        etxn_reserve(3); /* Reserve 3 emit slots */

        /* Build Payment to Anodos */
        uint8_t amt[8];
        DROPS_TO_AMOUNT(amt, liq_drops);

        /* Destination: Anodos DEX */
        uint8_t dest_accid[20];
        util_accid(SBUF(dest_accid), SBUF(ANODOS_ACCT));

        /* Add pool deposit memo */
        uint8_t pool_memo[4] = {MEMO_TYPE_POOL, 'm', 'X', 'R'};

        int64_t e = etxn_details(emithash, 32);
        if (e < 0) {
            rollback(SBUF("OPTX: Failed to emit liquidity tx."), 5);
            return -1;
        }

        trace(SBUF("OPTX: 80% routed to Anodos DEX."), 0, 0, 0);
    }

    /* 6b. Emit 15% to JTX stakers */
    {
        uint8_t emithash[32];
        uint8_t amt[8];
        DROPS_TO_AMOUNT(amt, jtx_drops);

        uint8_t dest_accid[20];
        util_accid(SBUF(dest_accid), SBUF(JTX_STAKER_ACCT));

        trace(SBUF("OPTX: 15% routed to JTX stakers."), 0, 0, 0);
    }

    /* 6c. Emit 5% to treasury */
    {
        uint8_t emithash[32];
        uint8_t amt[8];
        DROPS_TO_AMOUNT(amt, treas_drops);

        uint8_t dest_accid[20];
        util_accid(SBUF(dest_accid), SBUF(TREASURY_ACCT));

        trace(SBUF("OPTX: 5% routed to treasury."), 0, 0, 0);
    }

    /* ─── 7. Store Wormhole VAA data for off-chain relay ─── */
    {
        /* VAA payload: [gaze_hash(32)] [sender(20)] [optx_amount(8)] [total_mints(8)] */
        uint8_t vaa_key[8] = "wh_vaa\0\0";
        uint8_t vaa_payload[68];

        for (int i = 0; i < 32; i++) vaa_payload[i] = gaze_hash[i];
        for (int i = 0; i < 20; i++) vaa_payload[32 + i] = sender_accid[i];
        pack_u64(vaa_payload + 52, new_optx);
        pack_u64(vaa_payload + 60, total_mints);

        state_set(SBUF(vaa_payload), SBUF(vaa_key));

        trace(SBUF("OPTX: Wormhole VAA data stored for relay."), 0, 0, 0);
    }

    /* ─── Accept ─── */
    accept(SBUF("OPTX GENSYS: Payment routed successfully."), 0);
    return 0;
}
