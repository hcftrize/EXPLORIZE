"""Postgres access. All writes for a block range happen in ONE transaction,
together with the checkpoint update, so the database is never half-synced."""

from __future__ import annotations

import psycopg2
import psycopg2.extras

from .config import SCHEMA_FILE, Chain

LOCK_NAMESPACE = 7_300_000  # advisory lock key = namespace + chain_id


def connect(url: str):
    conn = psycopg2.connect(url, connect_timeout=20, application_name="explorize-indexer")
    conn.autocommit = False
    return conn


def init_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(SCHEMA_FILE.read_text())
    conn.commit()


def ensure_chains(conn, chains: list[Chain]) -> None:
    with conn.cursor() as cur:
        for c in chains:
            cur.execute(
                """INSERT INTO chain_state (chain_id, slug, token, start_block)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (chain_id) DO UPDATE
                     SET slug = EXCLUDED.slug, token = EXCLUDED.token,
                         start_block = COALESCE(EXCLUDED.start_block, chain_state.start_block)""",
                (c.chain_id, c.key, psycopg2.Binary(c.token_bytes), c.start_block),
            )
    conn.commit()


def try_lock(conn, chain_id: int) -> bool:
    """Session-level advisory lock: only one indexer per chain at a time.
    Needs a DIRECT connection (not the pgbouncer '-pooler' one)."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_NAMESPACE + chain_id,))
        ok = cur.fetchone()[0]
    conn.commit()
    return bool(ok)


def get_state(conn, chain_id: int) -> tuple[int | None, int | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT start_block, last_block FROM chain_state WHERE chain_id = %s", (chain_id,))
        row = cur.fetchone()
    conn.commit()
    return (row[0], row[1]) if row else (None, None)


def set_start_block(conn, chain_id: int, start_block: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE chain_state SET start_block = %s, updated_at = now() WHERE chain_id = %s",
                    (start_block, chain_id))
    conn.commit()


_INSERT = """
INSERT INTO transfers (chain_id, block_number, block_time, tx_hash, log_index, from_addr, to_addr, amount)
VALUES %s
ON CONFLICT (chain_id, tx_hash, log_index) DO UPDATE SET
  block_number = EXCLUDED.block_number, block_time = EXCLUDED.block_time,
  from_addr = EXCLUDED.from_addr, to_addr = EXCLUDED.to_addr, amount = EXCLUDED.amount
"""


def replace_range(conn, chain_id: int, lo: int, hi: int, rows: list[tuple], last_block: int) -> None:
    """Atomically: delete rows of [lo, hi] (handles reorged-away logs), insert the
    fresh ones, move the checkpoint. Re-running the same range is harmless."""
    dedup: dict[tuple, tuple] = {}
    for r in rows:  # r = (chain_id, block, ts, tx_hash, log_index, from, to, amount)
        dedup[(r[0], r[3], r[4])] = r
    payload = [
        (r[0], r[1], r[2], psycopg2.Binary(r[3]), r[4], psycopg2.Binary(r[5]), psycopg2.Binary(r[6]), r[7])
        for r in dedup.values()
    ]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM transfers WHERE chain_id = %s AND block_number BETWEEN %s AND %s",
                    (chain_id, lo, hi))
        if payload:
            psycopg2.extras.execute_values(
                cur, _INSERT, payload,
                template="(%s, %s, to_timestamp(%s), %s, %s, %s, %s, %s)", page_size=2000)
        cur.execute(
            "UPDATE chain_state SET last_block = %s, last_synced_at = now(), updated_at = now() WHERE chain_id = %s",
            (last_block, chain_id))
    conn.commit()


def supply_from_transfers(conn, chain_id: int, up_to_block: int) -> int:
    """minted - burned, from our own data (mint: from 0x0, burn: to 0x0)."""
    zero = psycopg2.Binary(bytes(20))
    with conn.cursor() as cur:
        cur.execute(
            """SELECT COALESCE(SUM(amount) FILTER (WHERE from_addr = %s), 0)
                    - COALESCE(SUM(amount) FILTER (WHERE to_addr = %s), 0)
               FROM transfers WHERE chain_id = %s AND block_number <= %s""",
            (zero, zero, chain_id, up_to_block))
        val = cur.fetchone()[0]
    conn.commit()
    return int(val)


def chain_stats(conn, chain_id: int) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), MIN(block_time), MAX(block_time) FROM transfers WHERE chain_id = %s",
                    (chain_id,))
        n, tmin, tmax = cur.fetchone()
    conn.commit()
    return {"rows": n, "first": tmin, "last": tmax}
