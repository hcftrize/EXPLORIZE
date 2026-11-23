"""Targeted reprocessing: re-read ONLY the transactions you point at, after you changed labels or pools.

    python -m indexer reprocess --chain bsc --reasons unknown_pool,guess,flag
    python -m indexer reprocess --pool 0x32a805e65a3219a7 --address 0xabc...,0xdef... --flags price-implausible
    python -m indexer reprocess --tx 0x<hash>,0x<hash> --tx-file corrections/transactions.txt --dry-run

The enrichment stores the reading of a transaction once and only moves forward, so a label or a pool added later does not
change what was already stored. This command re-reads the receipts of the selected transactions and rewrites their rows with
the same code as the enrichment (idempotent: running it twice changes nothing more). Selectors add up (union):

  --reasons   transactions waiting in `enrich_review` for these reasons: unknown_pool, guess, flag, crash
              (used alone, the default is unknown_pool,guess,flag; "none" switches this selector off)
  --pool      pool address (0x + 40 hex) or pool id (0x + 64 hex, or the first 18 characters shown by the SQL queries):
              review rows, liquidity moves and trades that mention it; for an address, also every stored transfer that touches it
              (a pool that was added to labels/pools.json after its transactions had passed)
  --address   router / relayer / signer / trader / liquidity provider: the trades and liquidity moves it appears in;
              when it is a registered pool or venue address, also every stored transfer that touches it
  --flags     trades carrying one of these flags (for instance price-implausible)
  --tx / --tx-file   an explicit list of transactions (the special cases): a hash, or "chain hash", one per line, # = comment

Nothing is written with --dry-run: it only says what would be re-read. The write of each batch takes the lock of the unified
run, so a correction never writes the same rows as a unified run at the same moment.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from pathlib import Path

import psycopg2

from . import enrich
from .registry import Registry

REASONS = ("unknown_pool", "guess", "flag", "crash")
DEFAULT_REASONS = ("unknown_pool", "guess", "flag")
HASH_RE = re.compile(r"^0x[0-9a-f]{64}$")
HEX_RE = re.compile(r"^0x[0-9a-f]+$")
SNAP_CHUNK = 5000


class Selection:
    """What to re-read: the union of the selectors (all empty = nothing)."""

    def __init__(self, reasons=(), pools=(), addresses=(), flags=(), txs=()):
        self.reasons = tuple(reasons)
        self.pools = tuple(pools)
        self.addresses = tuple(addresses)
        self.flags = tuple(flags)
        self.txs = tuple(txs)                          # (chain slug or None, 0x hash)

    def empty(self) -> bool:
        return not (self.reasons or self.pools or self.addresses or self.flags or self.txs)


def _split(text: str | None) -> list[str]:
    return [x.strip().lower() for x in re.split(r"[,\s;]+", text or "") if x.strip()]


def parse_tx_text(text: str) -> list[tuple[str | None, str]]:
    """Lines of `0xhash` or `chain 0xhash`; `#` starts a comment."""
    out = []
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip().lower()
        if not line:
            continue
        parts = re.split(r"[\s:,;]+", line)
        h = parts[-1]
        if not HASH_RE.match(h):
            raise SystemExit(f"transaction list, line {n}: '{raw.strip()[:80]}' is not a transaction hash (0x + 64 hex)")
        chain = parts[0] if len(parts) > 1 else None
        out.append((chain, h))
    return out


def build_selection(reasons: str | None, pools: str | None, addresses: str | None, flags: str | None,
                    txs: str | None, tx_file: str | None) -> Selection:
    pool_l, addr_l, flag_l = _split(pools), _split(addresses), _split(flags)
    for p in pool_l:
        if not HEX_RE.match(p) or not (len(p) == 42 or 18 <= len(p) <= 66):
            raise SystemExit(f"--pool {p}: expected a pool address (0x + 40 hex) or a pool id (0x + 64 hex, or its first 18 characters)")
    for a in addr_l:
        if not re.match(r"^0x[0-9a-f]{40}$", a):
            raise SystemExit(f"--address {a}: expected an address (0x + 40 hex)")
    tx_l = parse_tx_text((txs or "").replace(",", "\n"))
    if tx_file:
        path = Path(tx_file)
        if not path.exists():
            raise SystemExit(f"--tx-file {tx_file}: the file does not exist")
        tx_l += parse_tx_text(path.read_text(encoding="utf-8"))
    r = _split(reasons)
    if r == ["none"]:
        r = []
    elif not r and reasons is None and not (pool_l or addr_l or flag_l or tx_l):
        r = list(DEFAULT_REASONS)                      # no selector at all: the review queue
    bad = [x for x in r if x not in REASONS]
    if bad:
        raise SystemExit(f"--reasons {','.join(bad)}: choose among {', '.join(REASONS)} (or none)")
    return Selection(r, pool_l, addr_l, flag_l, tx_l)


def _bin(h: str):
    return psycopg2.Binary(bytes.fromhex(h[2:]))


def _rows(cur) -> list[tuple[str, int, object]]:
    return [("0x" + h, b, ts) for h, b, ts in cur.fetchall()]


def select_txs(conn, chain, reg: Registry, sel: Selection, log=print) -> tuple[list[tuple[str, int, object]], dict]:
    """The transactions of this chain to re-read: [(hash, block, time)] by block, and how many each selector brought."""
    cid = chain.chain_id
    found: dict[str, tuple[str, int, object]] = {}
    per: Counter = Counter()
    skipped = 0

    def add(source: str, rows) -> None:
        nonlocal skipped
        for h, b, ts in rows:
            if ts is None:
                skipped += 1                           # no stored transfer: nothing to take the time from
                continue
            per[source] += 1
            found.setdefault(h, (h, b, ts))

    with conn.cursor() as cur:
        if sel.reasons:
            cur.execute("""SELECT encode(r.tx_hash,'hex'), r.block_number,
                                  (SELECT min(t.block_time) FROM transfers t WHERE t.chain_id = r.chain_id AND t.tx_hash = r.tx_hash)
                             FROM enrich_review r WHERE r.chain_id = %s AND r.reason = ANY(%s)""", (cid, list(sel.reasons)))
            add("reasons", _rows(cur))
        for p in sel.pools:
            like_id = f"%{p[:18]}%"
            cur.execute("""SELECT encode(r.tx_hash,'hex'), r.block_number,
                                  (SELECT min(t.block_time) FROM transfers t WHERE t.chain_id = r.chain_id AND t.tx_hash = r.tx_hash)
                             FROM enrich_review r WHERE r.chain_id = %s AND lower(coalesce(r.detail,'')) LIKE %s""", (cid, like_id))
            add("pool", _rows(cur))
            cur.execute("""SELECT DISTINCT encode(tx_hash,'hex'), block_number, block_time FROM liquidity_events
                            WHERE chain_id = %s AND encode(pool_key,'hex') LIKE %s""", (cid, p[2:] + "%"))
            add("pool", _rows(cur))
            cur.execute("""SELECT encode(tx_hash,'hex'), block_number, block_time FROM trades
                            WHERE chain_id = %s AND fills::text LIKE %s""", (cid, f"%{p}%"))
            add("pool", _rows(cur))
            if len(p) == 42:
                cur.execute("""SELECT encode(tx_hash,'hex'), min(block_number), min(block_time) FROM transfers
                                WHERE chain_id = %s AND (from_addr = %s OR to_addr = %s) GROUP BY tx_hash""",
                            (cid, _bin(p), _bin(p)))
                add("pool", _rows(cur))
        for a in sel.addresses:
            cur.execute("""SELECT encode(tx_hash,'hex'), block_number, block_time FROM trades
                            WHERE chain_id = %s AND (trader = %s OR sender = %s OR router = %s)""", (cid, _bin(a), _bin(a), _bin(a)))
            add("address", _rows(cur))
            cur.execute("""SELECT DISTINCT encode(tx_hash,'hex'), block_number, block_time FROM liquidity_events
                            WHERE chain_id = %s AND provider = %s""", (cid, _bin(a)))
            add("address", _rows(cur))
            if reg.pool(chain.key, a) is not None or a in reg.holders(chain.key) or a in reg.rfq_addresses(chain.key):
                cur.execute("""SELECT encode(tx_hash,'hex'), min(block_number), min(block_time) FROM transfers
                                WHERE chain_id = %s AND (from_addr = %s OR to_addr = %s) GROUP BY tx_hash""",
                            (cid, _bin(a), _bin(a)))
                add("address", _rows(cur))
        if sel.flags:
            cur.execute("""SELECT encode(tx_hash,'hex'), block_number, block_time FROM trades
                            WHERE chain_id = %s AND flags && %s::text[]""", (cid, list(sel.flags)))
            add("flags", _rows(cur))
        missing = []
        for c, h in sel.txs:
            if c not in (None, chain.key):
                continue
            cur.execute("""SELECT min(block_number), min(block_time) FROM transfers WHERE chain_id = %s AND tx_hash = %s""",
                        (cid, _bin(h)))
            b, ts = cur.fetchone()
            if b is None:
                missing.append(h)
                continue
            add("list", [(h, b, ts)])
    conn.rollback()
    for h in missing:
        if any(c == chain.key and hh == h for c, hh in sel.txs):      # only when the list names this chain: else it may be on another one
            log(f"[{chain.key}] {h}: no stored RIZE transfer on this chain (nothing to re-read)")
    if skipped:
        log(f"[{chain.key}] {skipped} selected rows have no stored transfer to take the time from: skipped")
    rows = sorted(found.values(), key=lambda x: (x[1], x[0]))
    return rows, dict(per)


def snapshot_rows(conn, chain_id: int, hashes: list[str]) -> dict[str, tuple]:
    """What is stored about these transactions: trade reading, liquidity moves, review reason."""
    out: dict[str, tuple] = {}
    with conn.cursor() as cur:
        for i in range(0, len(hashes), SNAP_CHUNK):
            arr = [_bin(h) for h in hashes[i:i + SNAP_CHUNK]]
            cur.execute("""SELECT encode(tx_hash,'hex'), side, trader, router, rize, price_usd, flags FROM trades
                            WHERE chain_id = %s AND tx_hash = ANY(%s::bytea[])""", (chain_id, arr))
            trades = {"0x" + h: (s, bytes(t) if t else None, bytes(r) if r else None, str(z), str(p), tuple(f or ()))
                      for h, s, t, r, z, p, f in cur.fetchall()}
            cur.execute("""SELECT encode(tx_hash,'hex'), count(*) FROM liquidity_events
                            WHERE chain_id = %s AND tx_hash = ANY(%s::bytea[]) GROUP BY 1""", (chain_id, arr))
            liq = {"0x" + h: n for h, n in cur.fetchall()}
            cur.execute("""SELECT encode(tx_hash,'hex'), reason FROM enrich_review
                            WHERE chain_id = %s AND tx_hash = ANY(%s::bytea[])""", (chain_id, arr))
            rev = {"0x" + h: r for h, r in cur.fetchall()}
            for h in hashes[i:i + SNAP_CHUNK]:
                out[h] = (trades.get(h), liq.get(h, 0), rev.get(h))
    conn.rollback()
    return out


def compare(before: dict, after: dict) -> dict:
    """How the rewrite changed what is stored (counts only)."""
    res = Counter()
    for h, b in before.items():
        a = after.get(h)
        if a is None:
            continue
        bt, bl, br = b
        at, al, ar = a
        if bt is None and at is not None:
            res["became_trades"] += 1
        elif bt is not None and at is None:
            res["no_longer_trades"] += 1
        elif bt is not None and at is not None and bt != at:
            res["trades_changed"] += 1
        if bl != al:
            res["liquidity_changed"] += 1
        if br and not ar:
            res["left_review"] += 1
        elif br != ar:
            res["review_reason_changed" if br and ar else "entered_review"] += 1
    return dict(res)


def review_counts(conn, chain_id: int) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT reason, count(*) FROM enrich_review WHERE chain_id = %s GROUP BY 1", (chain_id,))
        out = {r: n for r, n in cur.fetchall()}
    conn.rollback()
    return out


def reprocess_chain(chain, client, conn, reg: Registry, sel: Selection, *, dry_run: bool = False,
                    max_seconds: float | None = None, log=print) -> dict:
    from .unified import ADVISORY_KEY
    t0 = time.monotonic()
    rows, per = select_txs(conn, chain, reg, sel, log)
    detail = ", ".join(f"{k} {v}" for k, v in sorted(per.items())) or "nothing"
    log(f"[{chain.key}] to re-read: {len(rows)} transactions ({detail}; a transaction found by several selectors counts once)")
    if not rows:
        return {"selected": 0, "rewritten": 0}
    if dry_run:
        for h, b, ts in rows[:10]:
            log(f"  [{chain.key}] would re-read {h}  block {b}  {ts}")
        if len(rows) > 10:
            log(f"  ... and {len(rows) - 10} more. Dry run: nothing was read from the node and nothing was written.")
        return {"selected": len(rows), "rewritten": 0, "dry_run": True}
    hashes = [h for h, *_ in rows]
    before = snapshot_rows(conn, chain.chain_id, hashes)
    review_before = review_counts(conn, chain.chain_id)
    done: list[str] = []
    for i in range(0, len(rows), enrich.RECEIPT_CHUNK):
        if max_seconds is not None and time.monotonic() - t0 > max_seconds:
            log(f"[{chain.key}] time budget reached after {len(done)} transactions; run the correction again to continue")
            break
        part = rows[i:i + enrich.RECEIPT_CHUNK]
        items = enrich.process_txs(chain, client, conn, reg, part, log, lock_key=ADVISORY_KEY)
        done += [h for h, *_ in items]
        if (i // enrich.RECEIPT_CHUNK) % 10 == 9:
            log(f"[{chain.key}] {len(done)} / {len(rows)} transactions re-read")
    after = snapshot_rows(conn, chain.chain_id, done)
    changed = compare({h: before[h] for h in done}, after)
    review_after = review_counts(conn, chain.chain_id)
    log(f"[{chain.key}] re-read {len(done)} of {len(rows)} transactions. What changed: "
        + (", ".join(f"{k.replace('_', ' ')} {v}" for k, v in sorted(changed.items())) or "nothing (the readings are the same)"))
    log(f"[{chain.key}] waiting for review (reason: before -> after): "
        + (", ".join(f"{r}: {review_before.get(r, 0)} -> {review_after.get(r, 0)}"
                     for r in sorted(set(review_before) | set(review_after))) or "empty"))
    return {"selected": len(rows), "rewritten": len(done), "changed": changed}
