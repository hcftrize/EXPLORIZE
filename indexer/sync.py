"""Backfill + incremental sync (same code path).

First run: scan from the token deployment block to the chain head, chunk by
chunk, saving a checkpoint after every chunk (so it can be stopped and resumed).
Next runs: restart a few blocks BEFORE the checkpoint (reorg margin) and scan to
the head again. Each chunk is delete+insert in one transaction, so re-scanning
is idempotent and logs removed by a reorg disappear.
"""

from __future__ import annotations

import time

from . import db
from .config import Chain, env_int
from .logs import TRANSFER_TOPIC0, decode_transfer, fetch_logs_adaptive
from .rpc import RpcError


def find_start_block(client, chain: Chain, head: int, log=print) -> int:
    """Smallest block where the token contract has code (deployment block),
    by binary search on eth_getCode. Needs an ARCHIVE node."""
    def has_code(n: int) -> bool:
        return client.get_code(chain.token, n) not in ("0x", "0x0", None, "")

    if not has_code(head):
        raise SystemExit(f"[{chain.key}] no contract code at {chain.token} on head: wrong address or chain?")
    lo, hi = 0, head  # invariant: no code at lo (or lo=0), code at hi
    try:
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if has_code(mid):
                hi = mid
            else:
                lo = mid
    except Exception as e:  # noqa: BLE001
        raise SystemExit(
            f"[{chain.key}] find-start failed ({type(e).__name__}: {e}). "
            "This needs an archive node (dRPC Pay As You Go). "
            "Or set start_block manually in config/chains.json.")
    log(f"[{chain.key}] deployment block found: {hi}")
    return hi


MIN_VALID_TS = 1_400_000_000  # anything before 2014 is a node placeholder (some answer 0x0)


def _valid_ts(ts) -> bool:
    return ts is not None and ts >= MIN_VALID_TS


def _fetch_with_retry(client, chain: Chain, topics, lo, hi, log_cap, stats, log, attempts: int = 4):
    """A long backfill must not die on one hiccup of the node: retry the chunk."""
    for attempt in range(1, attempts + 1):
        try:
            return fetch_logs_adaptive(client, chain.token, topics, lo, hi, log_cap, stats)
        except RpcError as e:
            if attempt == attempts:
                raise
            wait = 15 * attempt
            log(f"[{chain.key}] RPC error on blocks {lo:,}-{hi:,} ({e}); "
                f"retry {attempt}/{attempts - 1} in {wait}s")
            time.sleep(wait)


def sync_chain(chain: Chain, client, conn, *, max_seconds: float | None = None,
               to_block: int | None = None, log=print) -> dict:
    t0 = time.monotonic()
    log_cap = env_int("LOG_CAP", 10000)
    db.ensure_chains(conn, [chain])

    if not db.try_lock(conn, chain.chain_id):
        log(f"[{chain.key}] another indexer is already running for this chain, skipping")
        return {"chain": chain.key, "skipped": True}

    start_block, last_block = db.get_state(conn, chain.chain_id)
    head = client.block_number()
    target = min(to_block, head) if to_block is not None else head

    if last_block is None:
        if start_block is None:
            start_block = find_start_block(client, chain, head, log)
            db.set_start_block(conn, chain.chain_id, start_block)
        cursor = start_block
        log(f"[{chain.key}] first sync from block {cursor:,} to {target:,}")
    else:
        cursor = max(start_block or 0, last_block - chain.reorg_margin + 1)
        log(f"[{chain.key}] incremental sync from block {cursor:,} (checkpoint {last_block:,}, "
            f"margin {chain.reorg_margin}) to {target:,}")

    chunk = chain.initial_chunk
    ceiling = chain.max_chunk   # largest chunk size believed to work on this node
    ok_streak = 0
    total_rows = 0
    stopped_early = False
    topics = [TRANSFER_TOPIC0]

    while cursor <= target:
        if max_seconds is not None and time.monotonic() - t0 > max_seconds:
            stopped_early = True
            log(f"[{chain.key}] time budget reached, stopping cleanly (progress is saved)")
            break
        hi = min(cursor + chunk - 1, target)
        stats = {"splits": 0}
        raw = _fetch_with_retry(client, chain, topics, cursor, hi, log_cap, stats, log)
        decoded = [d for d in (decode_transfer(l) for l in raw) if d]

        missing = {d["block_number"] for d in decoded if not _valid_ts(d["block_ts"])}
        ts_map = client.get_block_timestamps(missing) if missing else {}
        rows = [
            (chain.chain_id, d["block_number"],
             d["block_ts"] if _valid_ts(d["block_ts"]) else ts_map[d["block_number"]],
             d["tx_hash"], d["log_index"], d["from"], d["to"], d["amount"])
            for d in decoded
        ]
        db.replace_range(conn, chain.chain_id, cursor, hi, rows, last_block=hi)
        total_rows += len(rows)
        log(f"[{chain.key}] blocks {cursor:,}-{hi:,}: +{len(rows)} transfers "
            f"(run total {total_rows:,}, chunk {chunk:,}, splits {stats['splits']}, "
            f"{(target - hi):,} blocks left)")
        cursor = hi + 1
        if stats["splits"] == 0:
            ok_streak += 1
            if ok_streak >= 100:  # from time to time, test whether the node accepts more again
                ceiling = min(ceiling * 2, chain.max_chunk)
                ok_streak = 0
            chunk = min(chunk * 2, ceiling)
        else:
            ok_streak = 0
            ceiling = max(chunk // 2, chain.min_chunk)  # do not retry the size that just failed
            chunk = ceiling

    return {"chain": chain.key, "skipped": False, "rows": total_rows,
            "stopped_early": stopped_early, "head": head,
            "reached": min(cursor - 1, target), "seconds": round(time.monotonic() - t0, 1)}
