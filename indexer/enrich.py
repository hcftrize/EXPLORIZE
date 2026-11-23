"""Enrichment: turn the receipts of the transactions that touch a RIZE venue into trades and liquidity moves.

    python -m indexer enrich   [--chain all|base|ethereum|bsc] [--max-minutes 20] [--rebuild]
    python -m indexer snapshot [--chain ...]        what every address pool holds right now
    python -m indexer discover [--chain ...]        busiest addresses that behave like RIZE pools but are not in the registry

Flow: transfers (already synced) -> the transactions touching a venue of the registry -> their receipts -> reconciliation +
venue adapters -> `trades` / `liquidity_events` (+ `enrich_review` for what could not be proven). It always resumes at
the checkpoint `enrich_state.last_block` (minus the reorg margin) and never goes beyond what the sync has stored, so it can be
chained after the sync at any frequency and catches up on its own after days without a run.

Only PROVEN readings become trades: venues given by Swap / fill events or by the registry. A transaction whose venue had to
be guessed from the flows goes to `enrich_review`, never to the trades table.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal

import psycopg2.extras

from . import config, db
from .multicall import SEL_BALANCE_OF, call_address, dec_address, dec_uint, multicall
from .receipts import ZERO, Recon, reconcile
from .registry import NATIVE, Pool, Registry
from .venues import decode_venues, refine

ENTRYPOINTS = {"0x0000000071727de22e5e9d8baf0edac6f37da032", "0x5ff137d4b0fdcd49dca30c7cf57e578a026d2789"}
WINDOW_BLOCKS = {"base": 20000, "ethereum": 5000, "bsc": 50000, "polygon": 20000}
# Stop before the plan's storage limit is reached (the Neon free plan: 1 GB). On a paid plan (no storage limit) set the repository
# variable STORAGE_LIMIT_MB (Settings > Secrets and variables > Actions > Variables). The workflows default to 4500 (5 GB plan).
STORAGE_LIMIT_MB = config.env_int("STORAGE_LIMIT_MB", 900)
STORAGE_LIMIT_SOURCE = "set by the STORAGE_LIMIT_MB variable" if os.environ.get("STORAGE_LIMIT_MB") else "built-in default: STORAGE_LIMIT_MB is not set"
RECEIPT_CHUNK = 60                # transactions fetched per round


def hx(a: str) -> bytes:
    return bytes.fromhex(a[2:])


MAX_PRICE = Decimal(10) ** 9      # dollars per RIZE; above this the ratio is dust, not a price (column is numeric(30,12))
MAX_USD = Decimal(10) ** 15


def key_bytes(k: str) -> bytes:
    return bytes.fromhex(k[2:])


@dataclass
class Built:
    trade: dict | None = None
    liquidity: list = field(default_factory=list)
    review: dict | None = None
    unknown_pools: list = field(default_factory=list)       # addresses certain to be RIZE pools but not in the registry


# ------------------------------------------------------------------------------------------ pure part
def _usd(fill_counter: dict, stables: set, meta: dict) -> Decimal | None:
    """Dollars of one fill, only when EVERY counter asset is a listed stablecoin (never an estimate)."""
    if not fill_counter or any(t not in stables for t in fill_counter):
        return None
    return sum((Decimal(x) / Decimal(10) ** meta.get(t, ("", 18))[1] for t, x in fill_counter.items()), Decimal(0))


def build(chain: str, reg: Registry, receipt: dict, info: dict) -> Built:
    """One transaction -> what to store. No network, no database."""
    rize = reg.rize(chain)
    frm = (info.get("from") or receipt.get("from") or "").lower()
    to = (info.get("to") or receipt.get("to") or "").lower()
    value = int(info.get("value", "0x0"), 16) if info.get("value") else 0
    vi = decode_venues(receipt, chain, reg)
    pool_addrs = {p.key for p in reg.pools(chain) if not p.is_id}
    r = reconcile(receipt, rize, reg.wrapped(chain), tx_from=frm, tx_value=value,
                  venue_addrs=vi.venue_addrs, lp_extra=vi.lp_extra, known_pools=pool_addrs)
    refine(r, vi, chain, reg, rize)
    out = Built()
    meta, stables = reg.token_meta(chain), reg.stables(chain)
    holders = set(reg.holders(chain))
    moved = sum(abs(n.get(rize, 0)) for a, n in r.nets.items() if a != ZERO) // 2

    # pools we are certain about (a classic Swap / liquidity event and a RIZE balance change) but nobody named
    known = holders | reg.rfq_addresses(chain)
    for a in sorted(set(r.swap_pools) | set(r.lp_pools)):
        if a not in known and reg.pool(chain, a) is None and r.nets.get(a, {}).get(rize):
            out.unknown_pools.append(a)

    is_trade = r.verdict in ("sell", "buy", "arbitrage")
    is_liq = r.verdict.startswith("liquidity") or r.verdict == "fee_collect" or bool(r.liquidity)
    block = int(receipt["blockNumber"], 16)

    if is_trade and r.evidence == "events" and r.fills:
        out.trade = _trade_row(chain, reg, r, frm, to, value, meta, stables, rize)
    elif (is_trade or is_liq) and r.evidence != "events":
        out.review = {"reason": "guess", "verdict": r.verdict, "rize": moved,
                      "detail": r.note or "venue guessed from the flows"}
    if out.trade and r.flags:
        out.review = {"reason": "flag", "verdict": r.verdict, "rize": moved, "detail": "; ".join(r.flags)}
    elif not out.trade and r.flags and not out.review:
        out.review = {"reason": "unknown_pool" if any(f.startswith("unknown_pool_id") for f in r.flags) else "flag",
                      "verdict": r.verdict, "rize": moved, "detail": "; ".join(r.flags)}

    if r.evidence == "events":
        out.liquidity = _liquidity_rows(chain, reg, r, frm, rize)
    return out


def _trade_row(chain, reg, r: Recon, frm, to, value, meta, stables, rize) -> dict:
    fills, usd_total, usd_rize = [], Decimal(0), 0
    for f in r.fills:
        u = _usd(f["counter"], stables, meta)
        fills.append({"pool": f["pool"], "rize": str(f["rize"]), "dir": f["dir"], "usd": (str(u) if u is not None else None),
                      "counter": {t: str(x) for t, x in f["counter"].items()}})
        if u is not None:
            usd_total += u
            usd_rize += f["rize"]
    counter = {t: str(x) for t, x in r.gross_counter.items()}
    if r.verdict == "arbitrage":
        bought = sum(f["rize"] for f in r.fills if f["dir"] < 0)
        sold = sum(f["rize"] for f in r.fills if f["dir"] > 0)
        rize_amt = min(bought, sold)
        trader = frm
    else:
        rize_amt = abs(r.pool_rize)
        not_venues = [a for a in r.traders if reg.pool(chain, a) is None and a not in reg.holders(chain)]
        cand = sorted(not_venues, key=lambda a: -abs(r.nets.get(a, {}).get(rize, 0)))
        trader = cand[0] if cand else None
    priced = bool(usd_rize)
    price = (usd_total / (Decimal(usd_rize) / Decimal(10) ** 18)) if priced and r.verdict != "arbitrage" else None
    usd_val = usd_total if priced and r.verdict != "arbitrage" else None
    odd = []
    if price is not None and price >= MAX_PRICE:          # a few wei of RIZE against real dollars: no meaningful price
        price = None
        odd.append("price-implausible")
    if usd_val is not None and usd_val >= MAX_USD:
        usd_val = None
        odd.append("usd-implausible")
    tags = []
    if r.verdict == "arbitrage":
        tags.append("arbitrage")
    if len(r.fills) > 1:
        tags.append("multi-venue")
    if any((reg.pool(chain, f["pool"]) or Pool(chain, "", "", "", "")).kind == "rfq" for f in r.fills):
        tags.append("rfq")
    if NATIVE in r.gross_counter or value:
        tags.append("native-leg")
    if to in ENTRYPOINTS:
        tags.append("erc4337")
    if r.rize_fees:
        tags.append("skimmed")
    if priced and usd_rize < sum(f["rize"] for f in r.fills):
        tags.append("usd-partial")
    extra = {}
    if r.rize_fees:
        extra["rize_fees"] = [[a, str(x)] for a, x in r.rize_fees]
    if r.fees:
        extra["fees"] = [[a, t, str(x)] for a, t, x in r.fees]
    if r.profit:
        extra["profit"] = {t: str(x) for t, x in r.profit.items()}
    if r.hops:
        extra["hops"] = r.hops
    if r.native_in:
        extra["native_in"] = str(r.native_in)
    if r.native_out:
        extra["native_out"] = str(r.native_out)
    if value:
        extra["tx_value"] = str(value)
    return {"side": r.verdict, "trader": trader, "sender": frm or None, "router": to or None, "rize": rize_amt,
            "counter": counter, "usd": usd_val, "price_usd": price,
            "fills": fills, "swap_count": r.swap_count + sum(f.get("events", 0) for f in r.fills),
            "tags": tags, "flags": list(r.flags) + odd, "extra": extra}


def _liquidity_rows(chain, reg, r: Recon, frm, rize) -> list[dict]:
    rows = []
    provider = (r.traders[0] if r.traders else frm) or None
    for pool, idx, kind, a0, a1 in r.lp_events:                  # classic pools: amounts come from the event itself
        p = reg.pool(chain, pool)
        if p is None or a0 is None or not (p.token0 and p.token1) or rize not in (p.token0, p.token1):
            continue
        ra, ca = (a0, a1) if p.token0 == rize else (a1, a0)
        if not ra and not ca:
            continue                                            # a position "poke" with nothing in it
        sign = 1 if kind == "mint" else -1
        rows.append({"seq": idx, "pool": pool, "kind": {"mint": "add", "burn": "remove", "collect": "collect"}[kind],
                     "provider": provider, "rize": sign * ra,
                     "counter": ({p.counter_token(rize): str(sign * ca)} if ca else {}),
                     "detail": {"tx_verdict": r.verdict}})
    for lq in r.liquidity:                                       # singleton pools: ModifyLiquidity
        rows.append({"seq": lq["log"], "pool": lq["pool"], "kind": lq["kind"], "provider": provider,
                     "rize": lq.get("rize"), "counter": {t: str(x) for t, x in (lq.get("counter") or {}).items()},
                     "detail": {"tx_verdict": r.verdict, "tick_lower": lq["tick_lower"], "tick_upper": lq["tick_upper"],
                                "delta": str(lq["delta"]), "salt": str(lq["salt"])}})
    return rows


# ------------------------------------------------------------------------------------------ database part
def pool_to_row(chain_id: int, p: Pool) -> tuple:
    b = lambda a: psycopg2.Binary(key_bytes(a)) if a else None  # noqa: E731
    return (chain_id, psycopg2.Binary(key_bytes(p.key)), p.name, p.venue, p.kind, b(p.token0), b(p.token1), p.fee,
            p.tick_spacing, b(p.factory), b(p.gauge), b(p.manager), b(p.holder), p.status, p.source, p.note or None)


def sync_registry(conn, reg: Registry, chains: dict) -> None:
    """Labels files -> `pools` table (the owner's file wins), then the discovered pools of the table -> the registry."""
    by_id = {c.chain_id: c.key for c in chains.values()}
    with conn.cursor() as cur:
        for p in reg.pools():
            if p.source == "auto" or p.chain not in chains:
                continue
            cur.execute("""INSERT INTO pools (chain_id, pool_key, name, venue, kind, token0, token1, fee, tick_spacing, factory,
                                              gauge, manager, holder, status, source, note)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (chain_id, pool_key) DO UPDATE SET name=EXCLUDED.name, venue=EXCLUDED.venue,
                             kind=EXCLUDED.kind, token0=EXCLUDED.token0, token1=EXCLUDED.token1, fee=EXCLUDED.fee,
                             tick_spacing=EXCLUDED.tick_spacing, factory=EXCLUDED.factory, gauge=EXCLUDED.gauge,
                             manager=EXCLUDED.manager, holder=EXCLUDED.holder, status=EXCLUDED.status,
                             source=EXCLUDED.source, note=EXCLUDED.note, updated_at=now()""",
                        pool_to_row(chains[p.chain].chain_id, p))
        cur.execute("""SELECT chain_id, encode(pool_key,'hex'), name, venue, kind, encode(token0,'hex'), encode(token1,'hex'), fee,
                              tick_spacing, encode(factory,'hex'), encode(gauge,'hex'), encode(manager,'hex'),
                              encode(holder,'hex'), status, source, note FROM pools WHERE source = 'auto'""")
        rows = []
        for (cid, key, name, venue, kind, t0, t1, fee, ts, fac, gau, man, hol, st, src, note) in cur.fetchall():
            h = lambda v: ("0x" + v) if v else ""  # noqa: E731
            rows.append(dict(chain=by_id[cid], key="0x" + key, name=name, venue=venue, kind=kind, token0=h(t0),
                             token1=h(t1), fee=fee, tick_spacing=ts, factory=h(fac), gauge=h(gau), manager=h(man),
                             holder=h(hol), status=st, source=src, note=note or ""))
    conn.commit()
    reg.with_names_from([r for r in rows if r["chain"] in by_id.values()])
    sync_labels(conn, reg, chains)


def sync_labels(conn, reg: Registry, chains: dict) -> None:
    """Labels files -> the tables the site reads: `labels` (entities + pool names), `venues`, `tokens`. The files win."""
    ids = {c.key: c.chain_id for c in chains.values()}
    with conn.cursor() as cur:
        for e in reg.entities():
            cid = 0 if e["chain"] == "all" else ids.get(e["chain"])
            if cid is None:
                continue
            cur.execute("""INSERT INTO labels (address, chain_id, label, category, source, logo) VALUES (%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (address, chain_id) DO UPDATE SET label=EXCLUDED.label, category=EXCLUDED.category,
                             source=EXCLUDED.source, logo=EXCLUDED.logo""",
                        (psycopg2.Binary(key_bytes(e["address"])), cid, e["label"], e.get("category", "other"),
                         e.get("source"), e.get("logo") or None))
        for p in reg.pools():
            if p.chain not in ids or p.is_id:
                continue                                           # pool ids are not addresses: they are named by `pools`
            cur.execute("""INSERT INTO labels (address, chain_id, label, category, source, logo) VALUES (%s,%s,%s,'pool',%s,NULL)
                           ON CONFLICT (address, chain_id) DO UPDATE SET label=EXCLUDED.label, category='pool',
                             source=EXCLUDED.source""",
                        (psycopg2.Binary(key_bytes(p.key)), ids[p.chain], p.name, p.source))
        for key, v in reg.venues.items():
            cur.execute("""INSERT INTO venues (key, name, kind, logo) VALUES (%s,%s,%s,%s)
                           ON CONFLICT (key) DO UPDATE SET name=EXCLUDED.name, kind=EXCLUDED.kind, logo=EXCLUDED.logo""",
                        (key, v.get("name", key), v.get("kind", "dex"), v.get("logo") or None))
        for chain, cid in ids.items():
            for addr, (sym, dec) in reg.token_meta(chain).items():
                cur.execute("""INSERT INTO tokens (chain_id, address, symbol, decimals) VALUES (%s,%s,%s,%s)
                               ON CONFLICT (chain_id, address) DO UPDATE SET symbol=EXCLUDED.symbol, decimals=EXCLUDED.decimals""",
                            (cid, psycopg2.Binary(key_bytes(addr)), sym, dec))
    conn.commit()


def txs_in_window(conn, chain_id: int, lo: int, hi: int, holders: set[str]) -> list[tuple[str, int, object]]:
    """Distinct transactions of the block window with a RIZE transfer touching a venue holder."""
    if not holders:
        return []
    arr = [psycopg2.Binary(hx(a)) for a in sorted(holders)]
    with conn.cursor() as cur:
        cur.execute("""SELECT encode(tx_hash,'hex'), min(block_number), min(block_time) FROM transfers
                        WHERE chain_id = %s AND block_number BETWEEN %s AND %s
                          AND (from_addr = ANY(%s::bytea[]) OR to_addr = ANY(%s::bytea[]))
                        GROUP BY tx_hash ORDER BY min(block_number), tx_hash""", (chain_id, lo, hi, arr, arr))
        rows = [("0x" + h, b, t) for h, b, t in cur.fetchall()]
    conn.rollback()
    return rows


def db_size_mb(conn) -> float:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_database_size(current_database())")
        v = cur.fetchone()[0]
    conn.rollback()
    return v / 1048576


def write_results(conn, chain_id: int, items: list[tuple[str, int, object, Built]]) -> None:
    """Replace everything stored about these transactions (idempotent: re-running changes nothing)."""
    hashes = [psycopg2.Binary(hx(h)) for h, *_ in items]
    with conn.cursor() as cur:
        for table in ("trades", "liquidity_events", "enrich_review"):
            cur.execute(f"DELETE FROM {table} WHERE chain_id = %s AND tx_hash = ANY(%s::bytea[])", (chain_id, hashes))
        trades, liqs, reviews = [], [], []
        for h, block, ts, b in items:
            hb = psycopg2.Binary(hx(h))
            if b.trade:
                t = b.trade
                trades.append((chain_id, hb, block, ts, t["side"], psycopg2.Binary(hx(t["trader"])) if t["trader"] else None,
                               psycopg2.Binary(hx(t["sender"])) if t["sender"] else None,
                               psycopg2.Binary(hx(t["router"])) if t["router"] else None, t["rize"],
                               json.dumps(t["counter"]), t["usd"], t["price_usd"], json.dumps(t["fills"]), t["swap_count"],
                               t["tags"], t["flags"], json.dumps(t["extra"])))
            for L in b.liquidity:
                liqs.append((chain_id, hb, L["seq"], block, ts, psycopg2.Binary(key_bytes(L["pool"])), L["kind"],
                             psycopg2.Binary(hx(L["provider"])) if L["provider"] else None, L["rize"],
                             json.dumps(L["counter"]), json.dumps(L["detail"])))
            if b.review:
                v = b.review
                reviews.append((chain_id, hb, block, v["reason"], v["verdict"], v["rize"], v["detail"]))
        if trades:
            psycopg2.extras.execute_values(cur, """INSERT INTO trades (chain_id, tx_hash, block_number, block_time, side, trader,
                sender, router, rize, counter, usd, price_usd, fills, swap_count, tags, flags, extra) VALUES %s""", trades)
        if liqs:
            psycopg2.extras.execute_values(cur, """INSERT INTO liquidity_events (chain_id, tx_hash, seq, block_number, block_time,
                pool_key, kind, provider, rize, counter, detail) VALUES %s ON CONFLICT DO NOTHING""", liqs)
        if reviews:
            psycopg2.extras.execute_values(cur, """INSERT INTO enrich_review (chain_id, tx_hash, block_number, reason, verdict,
                rize, detail) VALUES %s""", reviews)
    conn.commit()


def safe_write(conn, chain_id: int, items, log=print):
    """Write a batch; if the database refuses it, write the transactions one by one and send the refused ones to review."""
    try:
        write_results(conn, chain_id, items)
        return items
    except psycopg2.Error as e:
        conn.rollback()
        log(f"[chain {chain_id}] batch refused by the database ({type(e).__name__}); writing one transaction at a time")
    out = []
    for item in items:
        h, block, ts, b = item
        try:
            write_results(conn, chain_id, [item])
            out.append(item)
        except psycopg2.Error as e:
            conn.rollback()
            msg = f"database refused this row: {type(e).__name__}: {str(e).splitlines()[0][:160]}"
            log(f"[chain {chain_id}] {h}: {msg}")
            fallback = (h, block, ts, Built(review={"reason": "crash", "verdict": None, "rize": 0, "detail": msg}))
            write_results(conn, chain_id, [fallback])
            out.append(fallback)
    return out


def get_enrich_state(conn, chain_id: int) -> tuple[int | None, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT last_block, txs_done FROM enrich_state WHERE chain_id = %s", (chain_id,))
        row = cur.fetchone()
    conn.rollback()
    return (row[0], row[1]) if row else (None, 0)


def set_enrich_state(conn, chain_id: int, last_block: int, done_inc: int) -> None:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO enrich_state (chain_id, last_block, txs_done) VALUES (%s, %s, %s)
                       ON CONFLICT (chain_id) DO UPDATE SET last_block = EXCLUDED.last_block,
                         txs_done = enrich_state.txs_done + %s, updated_at = now()""",
                    (chain_id, last_block, done_inc, done_inc))
    conn.commit()


def rebuild(conn, chain_id: int) -> None:
    with conn.cursor() as cur:
        for table in ("trades", "liquidity_events", "enrich_review", "enrich_state"):
            cur.execute(f"DELETE FROM {table} WHERE chain_id = %s", (chain_id,))
    conn.commit()


# ------------------------------------------------------------------------------------------ discovery of new pools
def register_unknowns(client, conn, chain, reg: Registry, addrs: list[str], log) -> list[Pool]:
    """Pools certain to be RIZE pools (they emitted Swap / liquidity events and moved RIZE) that the registry does not
    know. Named POOL UNKNW n: the owner renames them in labels/pools.json (the file always wins over this name)."""
    from .market_check import SEL, read_pool_tokens
    rize = reg.rize(chain.key)
    found = read_pool_tokens(client, addrs)
    ts = multicall(client, [(a, bytes.fromhex(SEL["tickSpacing"])) for a in addrs], group=100) if addrs else []
    out = []
    for a, (ok, b) in zip(addrs, ts):
        tok = found.get(a)
        kind = "v3" if ok and len(b) >= 32 else ("v2" if tok else "unknown")
        p = reg.register_unknown(chain.key, a, kind, tok[0] if tok else "", tok[1] if tok else "", (tok[2] or "") if tok else "")
        if tok and rize not in tok[:2]:
            log(f"  [{chain.key}] {a} emits swap events but neither token is RIZE; kept as {p.name} for review")
        with conn.cursor() as cur:
            cur.execute("""INSERT INTO pools (chain_id, pool_key, name, venue, kind, token0, token1, fee, tick_spacing, factory,
                              gauge, manager, holder, status, source, note)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING""",
                        pool_to_row(chain.chain_id, p))
        conn.commit()
        log(f"  [{chain.key}] new pool found: {a} -> {p.name} ({kind}{', factory ' + p.factory if p.factory else ''})")
        out.append(p)
    return out


def discover(client, conn, chain, reg: Registry, top: int, log) -> list[Pool]:
    """Busiest addresses of the transfers table that answer like a pool of RIZE and are not registered yet."""
    from .market_check import read_pool_tokens, top_addresses
    known = set(reg.holders(chain.key)) | reg.rfq_addresses(chain.key)
    cand = [a for a, _ in top_addresses(conn, chain.chain_id, top) if a not in known and reg.pool(chain.key, a) is None
            and reg.entity(chain.key, a) is None]
    toks = read_pool_tokens(client, cand)
    rize = reg.rize(chain.key)
    pools = [a for a, v in toks.items() if rize in v[:2]]
    log(f"[{chain.key}] discovery: {len(cand)} busy addresses checked, {len(pools)} behave like RIZE pools")
    return register_unknowns(client, conn, chain, reg, pools, log) if pools else []


# ------------------------------------------------------------------------------------------ the job
@contextmanager
def writer_lock(conn, key: int, log=print, wait_seconds: float = 1800.0):
    """Hold the advisory lock of the unified run while a batch is written: a manual correction never writes the same rows as
    a unified run at the same moment. The wait is bounded; the lock is held for the write only (a fraction of a second)."""
    t0 = time.monotonic()
    waited = False
    while True:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (key,))
            got = cur.fetchone()[0]
        conn.commit()
        if got:
            break
        if not waited:
            log("a unified run is writing: waiting for it to finish before writing the corrections")
            waited = True
        if time.monotonic() - t0 > wait_seconds:
            raise RuntimeError("the unified run did not release its lock in time; run the correction again")
        time.sleep(3)
    try:
        yield
    finally:
        try:
            conn.rollback()
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s)", (key,))
            conn.commit()
        except psycopg2.Error:
            conn.rollback()


def process_txs(chain, client, conn, reg: Registry, part, log=print, lock_key: int | None = None):
    """Fetch the receipts of some transactions, turn each into rows and write them (idempotent). part = [(hash, block, time)].
    lock_key: take that advisory lock around the write (manual corrections use the lock of the unified run)."""
    calls = []
    for h, *_ in part:
        calls += [("eth_getTransactionReceipt", [h]), ("eth_getTransactionByHash", [h])]
    res = client.batch(calls)
    items, unknown = [], []
    for n, (h, block, ts) in enumerate(part):
        receipt, info = res[2 * n], res[2 * n + 1] or {}
        if not receipt:
            items.append((h, block, ts, Built(review={"reason": "crash", "verdict": None, "rize": 0,
                                                      "detail": "receipt not found"})))
            continue
        try:
            b = build(chain.key, reg, receipt, info)
        except Exception as e:  # noqa: BLE001 - one bad transaction must not stop the run
            b = Built(review={"reason": "crash", "verdict": None, "rize": 0, "detail": f"{type(e).__name__}: {e}"})
        unknown += b.unknown_pools
        items.append((h, block, ts, b))
    if unknown:
        register_unknowns(client, conn, chain, reg, sorted(set(unknown)), log)
    if lock_key is None:
        return safe_write(conn, chain.chain_id, items, log)
    with writer_lock(conn, lock_key, log):
        return safe_write(conn, chain.chain_id, items, log)


def repair_traders(chain, client, conn, reg: Registry, *, max_seconds: float | None = None, log=print) -> dict:
    """Re-read only the stored trades whose "trader" is a pool or a venue holder (a mistake of the first version)."""
    t0 = time.monotonic()
    bad = {bytes.fromhex(p.key[2:]) for p in reg.pools(chain.key) if not p.is_id}
    bad |= {bytes.fromhex(a[2:]) for a in reg.holders(chain.key)}
    with conn.cursor() as cur:
        cur.execute("SELECT encode(tx_hash,'hex'), block_number, block_time FROM trades WHERE chain_id = %s AND trader = ANY(%s::bytea[]) "
                    "ORDER BY block_number", (chain.chain_id, [psycopg2.Binary(b) for b in bad]))
        rows = [("0x" + h, b, ts) for h, b, ts in cur.fetchall()]
        # transactions that crashed earlier (a receipt the node did not return that day...) get another try
        cur.execute("""SELECT encode(r.tx_hash,'hex'), r.block_number,
                              (SELECT min(t.block_time) FROM transfers t WHERE t.chain_id = r.chain_id AND t.tx_hash = r.tx_hash)
                         FROM enrich_review r WHERE r.chain_id = %s AND r.reason = 'crash' ORDER BY r.block_number""", (chain.chain_id,))
        crashed = [("0x" + h, b, ts) for h, b, ts in cur.fetchall() if ts is not None]
    conn.rollback()
    seen = {h for h, *_ in rows}
    rows += [x for x in crashed if x[0] not in seen]
    log(f"[{chain.key}] repair: {len(rows) - len(crashed)} trades have a pool as their trader, {len(crashed)} crashed transactions to retry")
    fixed = 0
    for i in range(0, len(rows), RECEIPT_CHUNK):
        if max_seconds is not None and time.monotonic() - t0 > max_seconds:
            log(f"[{chain.key}] time budget reached; run the repair again to continue")
            break
        fixed += len(process_txs(chain, client, conn, reg, rows[i:i + RECEIPT_CHUNK], log))
    log(f"[{chain.key}] repair done: {fixed} transactions re-read")
    return {"fixed": fixed, "found": len(rows)}


def enrich_chain(chain, client, conn, reg: Registry, *, max_seconds: float | None = None, log=print) -> dict:
    t0 = time.monotonic()
    start, synced = db.get_state(conn, chain.chain_id)
    if synced is None:
        log(f"[{chain.key}] nothing synced yet; run the transfers sync first")
        return {"txs": 0, "reason": "not synced"}
    last, _ = get_enrich_state(conn, chain.chain_id)
    lo = (start or 0) if last is None else max(start or 0, last - chain.reorg_margin)
    window = WINDOW_BLOCKS.get(chain.key, 20000)
    holders = set(reg.holders(chain.key)) | reg.rfq_addresses(chain.key)
    done = trades = liqs = reviews = 0
    log(f"[{chain.key}] enrich from block {lo} to {synced} ({len(holders)} venue addresses, {len(reg.pools(chain.key))} pools)")
    while lo <= synced:
        if max_seconds is not None and time.monotonic() - t0 > max_seconds:
            log(f"[{chain.key}] time budget reached; progress saved at block {lo - 1}")
            break
        if db_size_mb(conn) > STORAGE_LIMIT_MB:
            log(f"[{chain.key}] STOP: the database is above {STORAGE_LIMIT_MB} MB ({STORAGE_LIMIT_SOURCE}); nothing more is written")
            break
        hi = min(lo + window - 1, synced)
        txs = txs_in_window(conn, chain.chain_id, lo, hi, holders)
        for i in range(0, len(txs), RECEIPT_CHUNK):
            items = process_txs(chain, client, conn, reg, txs[i:i + RECEIPT_CHUNK], log)
            done += len(items)
            trades += sum(1 for *_, b in items if b.trade)
            liqs += sum(len(b.liquidity) for *_, b in items)
            reviews += sum(1 for *_, b in items if b.review)
        set_enrich_state(conn, chain.chain_id, hi, len(txs))
        log(f"[{chain.key}] blocks {lo}-{hi}: {len(txs)} transactions")
        lo = hi + 1
    res = {"txs": done, "trades": trades, "liquidity_rows": liqs, "to_review": reviews}
    log(f"[{chain.key}] done: {res}")
    return res


# ------------------------------------------------------------------------------------------ snapshots
def snapshot(chain, client, conn, reg: Registry, log=print) -> int:
    """One row per address pool and hour: RIZE and counter-token balance (singleton pools share a holder: not photographed)."""
    rize = reg.rize(chain.key)
    pools = [p for p in reg.pools(chain.key) if not p.is_id and p.kind != "rfq" and p.token0 and p.token1 and rize in (p.token0, p.token1)]
    rfq = [p for p in reg.pools(chain.key) if p.kind == "rfq"]
    targets = pools + rfq
    if not targets:
        return 0
    calls = []
    for p in targets:
        calls += [(rize, call_address(SEL_BALANCE_OF, p.key)), (p.counter_token(rize), call_address(SEL_BALANCE_OF, p.key))]
    res = multicall(client, calls, group=100)
    block = client.block_number()
    n = 0
    with conn.cursor() as cur:
        for i, p in enumerate(targets):
            (ok1, b1), (ok2, b2) = res[2 * i], res[2 * i + 1]
            if not ok1 or len(b1) < 32:
                continue
            cur.execute("""INSERT INTO pool_snapshots (chain_id, pool_key, ts, block_number, rize, counter_token, counter)
                           VALUES (%s, %s, date_trunc('hour', now()), %s, %s, %s, %s)
                           ON CONFLICT (chain_id, pool_key, ts) DO UPDATE SET block_number = EXCLUDED.block_number,
                             rize = EXCLUDED.rize, counter = EXCLUDED.counter""",
                        (chain.chain_id, psycopg2.Binary(key_bytes(p.key)), block, dec_uint(b1),
                         psycopg2.Binary(key_bytes(p.counter_token(rize))),
                         dec_uint(b2) if ok2 and len(b2) >= 32 else None))
            n += 1
    conn.commit()
    log(f"[{chain.key}] snapshot: {n} pools photographed at block {block}")
    return n


# ------------------------------------------------------------------------------------------ report (read-only diagnosis)
def report(conn, log=print) -> None:
    """What is stored, how far each chain got, how big the database is, and what went to review. Writes nothing."""
    with conn.cursor() as cur:
        cur.execute("SELECT pg_size_pretty(pg_database_size(current_database())), pg_database_size(current_database())/1048576")
        pretty, mb = cur.fetchone()
        log(f"DATABASE SIZE: {pretty} ({int(mb)} MB). The enrichment stops by itself above {STORAGE_LIMIT_MB} MB ({STORAGE_LIMIT_SOURCE}).")
        cur.execute("""SELECT relname, pg_size_pretty(pg_total_relation_size(c.oid)) FROM pg_class c
                         JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public' AND c.relkind = 'r'
                         ORDER BY pg_total_relation_size(c.oid) DESC LIMIT 12""")
        log("LARGEST TABLES (with their indexes): " + ", ".join(f"{a} {b}" for a, b in cur.fetchall()))
        cur.execute("""SELECT cs.chain_id, cs.slug, cs.last_block, es.last_block, es.txs_done FROM chain_state cs
                         LEFT JOIN enrich_state es USING (chain_id) ORDER BY cs.chain_id""")
        for cid, slug, synced, enriched, n in cur.fetchall():
            gap = (synced - enriched) if (synced is not None and enriched is not None) else None
            log(f"PROGRESS {slug}: transfers synced to block {synced}, enrichment at block {enriched}"
                + (f" ({gap:,} blocks behind)" if gap else " (up to date)" if gap == 0 else " (not started)"))
        cur.execute("SELECT chain_id, side, count(*) FROM trades GROUP BY 1, 2 ORDER BY 1, 2")
        log("TRADES: " + ", ".join(f"{c}/{s} {n:,}" for c, s, n in cur.fetchall()))
        cur.execute("SELECT chain_id, kind, count(*) FROM liquidity_events GROUP BY 1, 2 ORDER BY 1, 2")
        log("LIQUIDITY EVENTS: " + ", ".join(f"{c}/{k} {n:,}" for c, k, n in cur.fetchall()))
        cur.execute("SELECT chain_id, reason, coalesce(verdict,'-'), count(*) FROM enrich_review GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 20")
        log("TO REVIEW (chain / reason / verdict: count): " + "; ".join(f"{c}/{r}/{v}: {n:,}" for c, r, v, n in cur.fetchall()))
        cur.execute("""SELECT chain_id, left(coalesce(detail,''), 90), count(*) FROM enrich_review
                        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 12""")
        for c, d, n in cur.fetchall():
            log(f"  review detail  chain {c}  {n:,} x  {d}")
        cur.execute("""SELECT chain_id, encode(tx_hash,'hex'), left(coalesce(detail,''), 200) FROM enrich_review
                        WHERE reason = 'crash' ORDER BY seen_at DESC LIMIT 10""")
        for c, h, d in cur.fetchall():
            log(f"  CRASH chain {c}  tx 0x{h}  {d}")
        cur.execute("""SELECT unnest(flags) f, count(*) FROM trades GROUP BY 1 ORDER BY 2 DESC LIMIT 10""")
        log("TRADE FLAGS: " + ", ".join(f"{f} {n:,}" for f, n in cur.fetchall()))
        cur.execute("""SELECT count(*) FROM trades t JOIN pools p ON p.chain_id = t.chain_id AND p.pool_key = t.trader""")
        log(f"TRADES WHOSE TRADER IS A POOL (to repair): {cur.fetchone()[0]:,}")
    conn.rollback()
