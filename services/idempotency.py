"""
Idempotency + audit log for donor-reward triggers.

Keyed on the Solana donate_sol transaction signature. Each donation row
tracks the status of every reward leg (JTX drop, Xahau badge, Metaplex
receipt). Helius will retry webhook deliveries on non-2xx — this layer
ensures retries never double-pay.

Backed by SQLite (single-writer is fine for our throughput). Path is
configurable via `DONATIONS_DB_PATH` env var so tests can use ":memory:".
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from typing import Iterator, Literal


# Status values are stored as TEXT for grep-ability in production logs.
LegStatus = Literal["pending", "in_flight", "done", "failed", "skipped"]


@dataclass
class DonationRecord:
    sol_tx_sig: str
    donor_wallet: str
    lamports: int
    xahau_address: str | None
    created_at: float

    jtx_drop_status: LegStatus = "pending"
    jtx_drop_tx_sig: str | None = None
    jtx_drop_error: str | None = None
    jtx_drop_attempts: int = 0

    xahau_status: LegStatus = "skipped"  # default-skipped if no xahau_address
    xahau_tx_hash: str | None = None
    xahau_error: str | None = None
    xahau_attempts: int = 0

    receipt_status: LegStatus = "pending"  # claimable, future Phase 3b
    receipt_claimed_at: float | None = None

    def to_public_dict(self) -> dict:
        """Shape returned to the dapp frontend (no internal error stacks)."""
        return {
            "sol_tx_sig": self.sol_tx_sig,
            "donor_wallet": self.donor_wallet,
            "lamports": self.lamports,
            "rewards": {
                "jtx_drop": {
                    "status": self.jtx_drop_status,
                    "tx_sig": self.jtx_drop_tx_sig,
                },
                "xahau_badge": {
                    "status": self.xahau_status,
                    "tx_hash": self.xahau_tx_hash,
                    "address": self.xahau_address,
                },
                "metaplex_receipt": {
                    "status": self.receipt_status,
                    "claimed_at": self.receipt_claimed_at,
                },
            },
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS donations (
    sol_tx_sig          TEXT PRIMARY KEY,
    donor_wallet        TEXT NOT NULL,
    lamports            INTEGER NOT NULL,
    xahau_address       TEXT,
    created_at          REAL NOT NULL,

    jtx_drop_status     TEXT NOT NULL DEFAULT 'pending',
    jtx_drop_tx_sig     TEXT,
    jtx_drop_error      TEXT,
    jtx_drop_attempts   INTEGER NOT NULL DEFAULT 0,

    xahau_status        TEXT NOT NULL DEFAULT 'skipped',
    xahau_tx_hash       TEXT,
    xahau_error         TEXT,
    xahau_attempts      INTEGER NOT NULL DEFAULT 0,

    receipt_status      TEXT NOT NULL DEFAULT 'pending',
    receipt_claimed_at  REAL
);

CREATE INDEX IF NOT EXISTS donations_donor_wallet_idx
    ON donations(donor_wallet);

CREATE INDEX IF NOT EXISTS donations_created_at_idx
    ON donations(created_at);
"""


class IdempotencyStore:
    """SQLite-backed idempotency + status store.

    Thread-safety: SQLite serializes writers; we open a per-call connection
    rather than a shared one so we play nicely with FastAPI's threadpool
    and with pytest's ephemeral fixtures.
    """

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.getenv("DONATIONS_DB_PATH", "/var/lib/optx/donations.db")
        # `:memory:` databases are per-connection; we must hold one open for
        # the lifetime of the store. File-backed databases use per-call
        # connections so we play nicely with FastAPI's threadpool.
        self._memory_conn: sqlite3.Connection | None = None
        # Lock guarding the persistent in-memory connection (FastAPI
        # TestClient + BackgroundTasks use a worker threadpool). For
        # file-backed databases SQLite serializes writes internally and
        # we open a fresh connection per call, so no lock needed there.
        self._memory_lock = threading.Lock()
        if self.db_path == ":memory:":
            self._memory_conn = sqlite3.connect(
                self.db_path,
                isolation_level=None,
                check_same_thread=False,
            )
            self._memory_conn.row_factory = sqlite3.Row
        else:
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        if self._memory_conn is not None:
            with self._memory_lock:
                yield self._memory_conn
            return
        conn = sqlite3.connect(self.db_path, isolation_level=None)  # autocommit
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)

    # ── Core flows ────────────────────────────────────────────────────────

    def claim_or_fetch(
        self,
        sol_tx_sig: str,
        donor_wallet: str,
        lamports: int,
        xahau_address: str | None,
    ) -> tuple[DonationRecord, bool]:
        """Insert a new donation row or return the existing one.

        Returns:
          (record, is_new). If is_new is False, the caller should NOT
          re-trigger drops/badges — the existing record already captures
          their state.
        """
        now = time.time()
        with self._conn() as c:
            # SQLite ON CONFLICT DO NOTHING returns no rowcount info that's
            # portable, so we use a SELECT-then-INSERT pattern under the
            # autocommit isolation. Risk of race is tiny (one webhook
            # delivery at a time per tx_sig) and the PRIMARY KEY catches
            # it deterministically.
            existing = c.execute(
                "SELECT * FROM donations WHERE sol_tx_sig = ?",
                (sol_tx_sig,),
            ).fetchone()
            if existing is not None:
                return _row_to_record(existing), False

            try:
                c.execute(
                    """
                    INSERT INTO donations (
                        sol_tx_sig, donor_wallet, lamports, xahau_address,
                        created_at, xahau_status
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sol_tx_sig,
                        donor_wallet,
                        lamports,
                        xahau_address,
                        now,
                        "pending" if xahau_address else "skipped",
                    ),
                )
            except sqlite3.IntegrityError:
                # Lost a race; fall through to fetch.
                row = c.execute(
                    "SELECT * FROM donations WHERE sol_tx_sig = ?",
                    (sol_tx_sig,),
                ).fetchone()
                return _row_to_record(row), False

            row = c.execute(
                "SELECT * FROM donations WHERE sol_tx_sig = ?",
                (sol_tx_sig,),
            ).fetchone()
            return _row_to_record(row), True

    def get(self, sol_tx_sig: str) -> DonationRecord | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM donations WHERE sol_tx_sig = ?",
                (sol_tx_sig,),
            ).fetchone()
            return _row_to_record(row) if row else None

    def update_jtx_drop(
        self,
        sol_tx_sig: str,
        status: LegStatus,
        *,
        tx_sig: str | None = None,
        error: str | None = None,
        attempts_increment: int = 0,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                UPDATE donations
                SET jtx_drop_status = ?,
                    jtx_drop_tx_sig = COALESCE(?, jtx_drop_tx_sig),
                    jtx_drop_error  = ?,
                    jtx_drop_attempts = jtx_drop_attempts + ?
                WHERE sol_tx_sig = ?
                """,
                (status, tx_sig, error, attempts_increment, sol_tx_sig),
            )

    def update_xahau(
        self,
        sol_tx_sig: str,
        status: LegStatus,
        *,
        tx_hash: str | None = None,
        error: str | None = None,
        attempts_increment: int = 0,
    ) -> None:
        with self._conn() as c:
            c.execute(
                """
                UPDATE donations
                SET xahau_status = ?,
                    xahau_tx_hash = COALESCE(?, xahau_tx_hash),
                    xahau_error   = ?,
                    xahau_attempts = xahau_attempts + ?
                WHERE sol_tx_sig = ?
                """,
                (status, tx_hash, error, attempts_increment, sol_tx_sig),
            )

    def daily_jtx_drop_count(self) -> int:
        """Number of JTX drops attempted in the last 24h. Used for runaway-cap."""
        cutoff = time.time() - 86_400
        with self._conn() as c:
            row = c.execute(
                """
                SELECT COUNT(*) AS n FROM donations
                WHERE created_at >= ? AND jtx_drop_status IN ('done','in_flight')
                """,
                (cutoff,),
            ).fetchone()
            return int(row["n"])


def _row_to_record(row: sqlite3.Row) -> DonationRecord:
    return DonationRecord(
        sol_tx_sig=row["sol_tx_sig"],
        donor_wallet=row["donor_wallet"],
        lamports=int(row["lamports"]),
        xahau_address=row["xahau_address"],
        created_at=float(row["created_at"]),
        jtx_drop_status=row["jtx_drop_status"],
        jtx_drop_tx_sig=row["jtx_drop_tx_sig"],
        jtx_drop_error=row["jtx_drop_error"],
        jtx_drop_attempts=int(row["jtx_drop_attempts"]),
        xahau_status=row["xahau_status"],
        xahau_tx_hash=row["xahau_tx_hash"],
        xahau_error=row["xahau_error"],
        xahau_attempts=int(row["xahau_attempts"]),
        receipt_status=row["receipt_status"],
        receipt_claimed_at=row["receipt_claimed_at"],
    )


__all__ = ["IdempotencyStore", "DonationRecord", "LegStatus"]
