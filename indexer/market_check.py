"""Market diagnostic (read only): who are the venues behind the RIZE trades, and does the reconciliation read real swaps right?

    python -m indexer.market_check trade-check --txs 0xc66e28ce...,0x...   [--chain base]
    python -m indexer.market_check classify --top 40                       [--chain base] [--addresses 0x..,0x..]

trade-check  Fetches each transaction receipt and prints what the reconciliation concludes: verdict (sell / buy /
             liquidity / arbitrage / transfer ...), who traded, which pools filled it, fees, relays, price, and the
             raw transfers underneath. Use it to check the logic against transactions you know.
classify     Takes the busiest addresses of the transfers table (or the ones you give) and asks the chain what each
             one IS: wallet or contract, and for a contract: pool (token0 / token1 / fee / tick spacing / stable flag /
             reserves), factory address, name, owner, RIZE balance. The factory address is printed as is, so DEX
             names are identified from facts and never guessed.

Nothing is written anywhere. Environment: DRPC_API_KEY (or RPC_URL_<CHAIN>); DATABASE_URL optional (labels, top addresses).
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from . import config
from .multicall import SEL_BALANCE_OF, call_address, call_none, dec_address, dec_uint, multicall
from .receipts import ZERO, reconcile, report
from .registry import NATIVE, Registry
from .venues import decode_venues, refine
from .rpc import RpcClient, RpcError

DEFAULT_TX = "0xc66e28ce9a7ea945e829692196eb6271ff30ce2aaa167ced6a56cbc32782950c"

SEL = {
    "token0": "0dfe1681", "token1": "d21220a7", "factory": "c45a0155", "fee": "ddca3f43",
    "tickSpacing": "d0c93a7c", "stable": "22be3de1", "getReserves": "0902f1ac", "owner": "8da5cb5b",
    "liquidity": "1a686502", "name": "06fdde03", "symbol": "95d89b41", "decimals": "313ce567",
}


def short(a: str) -> str:
    return a[:8] + "…" + a[-6:]


def dec_string(b: bytes) -> str:
    """ABI string (offset, length, bytes) or a bytes32 string."""
    try:
        if len(b) >= 96 and int.from_bytes(b[:32], "big") == 32:
            n = int.from_bytes(b[32:64], "big")
            if n <= len(b) - 64:
                return b[64:64 + n].decode("utf-8", "replace")
        return b[:32].rstrip(b"\x00").decode("utf-8", "replace")
    except Exception:
        return ""


def read_token_meta(client, tokens, log) -> dict[str, tuple[str, int]]:
    """token address -> (symbol, decimals) through one multicall."""
    tokens = sorted(set(tokens))
    calls = []
    for t in tokens:
        calls += [(t, bytes.fromhex(SEL["symbol"])), (t, bytes.fromhex(SEL["decimals"]))]
    res = multicall(client, calls, group=100) if calls else []
    out = {}
    for i, t in enumerate(tokens):
        ok_s, sb = res[2 * i]
        ok_d, db_ = res[2 * i + 1]
        sym = dec_string(sb) if ok_s and sb else ""
        dec = dec_uint(db_) if ok_d and len(db_) >= 32 else 18
        out[t] = (sym or t[:8] + "…", dec if 0 <= dec <= 36 else 18)
    return out


def read_pool_tokens(client, pools) -> dict[str, tuple]:
    """pool -> (token0, token1, factory or None). Pools are recognised by the answers, nothing is assumed."""
    pools = sorted(set(pools))
    calls = []
    for a in pools:
        calls += [(a, bytes.fromhex(SEL["token0"])), (a, bytes.fromhex(SEL["token1"])), (a, bytes.fromhex(SEL["factory"]))]
    res = multicall(client, calls, group=120) if calls else []
    out = {}
    for i, a in enumerate(pools):
        (o0, b0), (o1, b1), (of, bf) = res[3 * i:3 * i + 3]
        if o0 and o1 and len(b0) >= 32 and len(b1) >= 32:
            out[a] = (dec_address(b0), dec_address(b1), dec_address(bf) if of and len(bf) >= 32 else None)
    return out


def load_labels(conn, addrs) -> dict[str, str]:
    if conn is None or not addrs:
        return {}
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT encode(address,'hex'), label FROM labels WHERE address = ANY(%s::bytea[])",
                        ([bytes.fromhex(a[2:]) for a in addrs],))
            rows = {"0x" + a: l for a, l in cur.fetchall()}
        conn.rollback()
        return rows
    except Exception:
        conn.rollback()
        return {}


def code_kinds(client, addrs) -> dict[str, str]:
    """address -> "wallet" | "smart-wallet" | "contract".
    A "smart-wallet" is an ordinary wallet that delegates to a smart-account code (EIP-7702: code is 0xef0100 + address);
    it is the user's own wallet, not a contract that holds funds for others."""
    addrs = sorted(set(addrs))
    if not addrs:
        return {}
    codes = client.batch([("eth_getCode", [a, "latest"]) for a in addrs])
    out = {}
    for a, c in zip(addrs, codes):
        c = (c or "0x").lower()
        out[a] = ("wallet" if c in ("0x", "0x0") else
                  "smart-wallet" if c.startswith("0xef0100") and len(c) == 48 else "contract")
    return out


def is_contract(client, addrs) -> dict[str, bool]:
    return {a: k == "contract" for a, k in code_kinds(client, addrs).items()}


# ------------------------------------------------------------------------------------------ trade-check
def trade_check(client, chain, txs, conn, log, brief: bool = False) -> bool:
    """One block of text per transaction, printed in one piece; a transaction that fails does not stop the others."""
    all_ok = True
    for tx in txs:
        buf: list[str] = []
        try:
            ok = _trade_check_one(client, chain, tx, conn, buf.append, brief)
        except Exception as e:  # noqa: BLE001 - diagnostic: say which transaction and why, then go on
            import traceback
            buf.append(f"  CRASHED on this transaction: {type(e).__name__}: {e}")
            buf.append("  " + traceback.format_exc().strip().replace("\n", "\n  "))
            ok = False
        log("\n".join(buf))
        all_ok = all_ok and ok
    return all_ok


def _trade_check_one(client, chain, tx, conn, log, brief: bool) -> bool:
    from . import enrich
    reg = Registry.from_dir()
    rize = chain.token
    wrapped = reg.wrapped(chain.key)
    stables = reg.stables(chain.key)
    if True:
        log("=" * 78)
        receipt = client.get_transaction_receipt(tx)
        if not receipt:
            log(f"TX {tx}  ({chain.name})\n  receipt not found on this chain (wrong chain or hash?)")
            return False
        info = client.call("eth_getTransactionByHash", [tx]) or {}
        value = int(info.get("value", "0x0"), 16)
        frm = (info.get("from") or receipt.get("from") or "").lower()
        to = (info.get("to") or receipt.get("to") or "").lower()
        # one single line: block, status, sender, target and value stay attached to the hash
        log(f"TX {tx}  ({chain.name}) block {int(receipt['blockNumber'], 16)} status "
            f"{'ok' if receipt.get('status') == '0x1' else 'FAILED'} sender {frm} to {to} native value {value / 1e18:g}")
        vi = decode_venues(receipt, chain.key, reg)
        r = reconcile(receipt, rize, wrapped, tx_from=frm, tx_value=value, venue_addrs=vi.venue_addrs, lp_extra=vi.lp_extra)
        refine(r, vi, chain.key, reg, rize)
        pool_info = read_pool_tokens(client, set(r.swap_pools) | set(r.lp_pools))
        tokens = ({t for _, t, *_ in r.transfers} | {t for n in r.nets.values() for t in n} | {rize}
                  | {x for v in pool_info.values() for x in v[:2]}) - {NATIVE}
        meta = {**read_token_meta(client, tokens, log), **reg.token_meta(chain.key)}
        addrs = list(r.nets) + r.relays + r.swap_pools + [frm, to]
        kinds = code_kinds(client, [a for a in set(addrs) if a != ZERO][:40])
        labels = load_labels(conn, [a for a in set(addrs) if a != ZERO])
        for a in set(addrs):
            if a != ZERO and a not in labels and reg.label(chain.key, a):
                labels[a] = reg.label(chain.key, a)
        short_l = lambda a: (f"{labels[a]} ({short(a)})" if a in labels else short(a))  # noqa: E731
        pool_tokens = {a: (v[0], v[1]) for a, v in pool_info.items()}
        for line in report(r, rize, meta, short=short_l, stables=stables, brief=brief, pool_tokens=pool_tokens):
            log("  " + line)
        log(f"  Evidence: {r.evidence}" + ("  (venues proven by Swap / fill events or the registry)" if r.evidence == "events"
                                           else "  (venues GUESSED from the flows: this is not stored as a trade)"))
        for holder, ev, net, ok in r.venue_checks:
            log(f"  Venue check {short_l(holder)}: events say {ev}, token movements say {net} -> {'OK' if ok else 'DIFFERENT'}")
        for f in r.flags:
            log(f"  FLAG: {f}")
        for lq in r.liquidity:
            p = reg.pool(chain.key, lq["pool"])
            amt = "" if lq.get("rize") is None else f", RIZE {lq['rize'] / 1e18:+,.6f}"
            log(f"  Liquidity move: {lq['kind']} in {p.name if p else short(lq['pool'])} ticks {lq['tick_lower']}..{lq['tick_upper']} "
                f"position {lq['salt']}{amt}")
        built = enrich.build(chain.key, reg, receipt, info)
        if built.trade:
            t = built.trade
            usd = f", ${t['usd']:,.6f} (${t['price_usd']:.8f} per RIZE)" if t["usd"] is not None else ", dollar value not computable without a guess"
            log(f"  WOULD BE STORED: trade {t['side']} {t['rize'] / 1e18:,.6f} RIZE, {len(t['fills'])} fill(s){usd}, tags {t['tags']}")
        elif built.liquidity:
            log(f"  WOULD BE STORED: {len(built.liquidity)} liquidity row(s)")
        elif built.review:
            log(f"  WOULD GO TO REVIEW: {built.review['reason']} ({built.review['detail']})")
        else:
            log("  WOULD BE STORED: nothing (not a trade or a liquidity move; the raw transfers stay in the transfers table)")
        for a in built.unknown_pools:
            log(f"  NEW POOL candidate: {a} emits swap events and moved RIZE but is not in labels/pools.json "
                "(the enrichment would register it as POOL UNKNW n)")
        for t, (sy, _) in meta.items():                 # a token that wears our symbol but is not our token
            if t != rize and sy.upper() == meta.get(rize, ("RIZE", 18))[0].upper():
                log(f"  WARNING: token {t} is called {sy!r} like RIZE but is NOT RIZE")
        if value and not brief:
            log(f"  Note: the transaction carried {value / 1e18:g} native coin; a trade paid or received in native "
                "ETH leaves no token log for that leg.")
        if pool_info:
            log("  Pools in this transaction (read from the chain):")
            for a, (t0, t1, fac) in sorted(pool_info.items()):
                log(f"    {a}  {meta.get(t0, ('?',))[0]}/{meta.get(t1, ('?',))[0]}  factory {fac or '(none)'}"
                    f"{'  ' + labels[a] if a in labels else ''}")
        if not brief:
            log("  Addresses (full), role and type:")
            for a in sorted(set(addrs) - {ZERO}):
                role = ("trader" if a in r.traders else "pool" if a in r.venues else "pool(Swap)" if a in r.swap_pools else
                        "relay" if a in r.relays else "route swap" if a in r.hops else "sender" if a == frm else "")
                log(f"    {a}  {kinds.get(a, '?')}  {role}  {labels.get(a, '')}")
    return True


def recent_txs(conn, chain_id: int, n: int, min_rize: int = 0) -> list[str]:
    """The n most recent transaction hashes of the transfers table on one chain (optionally only moves >= min_rize)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT encode(tx_hash, 'hex'), max(block_number) b FROM transfers
                        WHERE chain_id = %s AND amount >= %s AND from_addr <> %s AND to_addr <> %s
                        GROUP BY tx_hash ORDER BY b DESC LIMIT %s""",
                    (chain_id, min_rize * 10 ** 18, bytes(20), bytes(20), n))
        rows = ["0x" + h for h, _ in cur.fetchall()]
    conn.rollback()
    return rows


# -------------------------------------------------------------------------------------------- classify
def top_addresses(conn, chain_id, n):
    with conn.cursor() as cur:
        cur.execute("""
            SELECT encode(a, 'hex'), count(*) FROM (
                SELECT from_addr AS a FROM transfers WHERE chain_id = %s
                UNION ALL SELECT to_addr FROM transfers WHERE chain_id = %s) x
            WHERE a <> %s GROUP BY a ORDER BY count(*) DESC LIMIT %s""",
                    (chain_id, chain_id, bytes(20), n))
        rows = [("0x" + a, c) for a, c in cur.fetchall()]
    conn.rollback()
    return rows


def classify(client, chain, addrs: list[tuple[str, int | None]], conn, log) -> bool:
    rize = chain.token
    kinds = is_contract(client, [a for a, _ in addrs])
    contracts = [a for a, _ in addrs if kinds.get(a)]
    names = ["token0", "token1", "factory", "fee", "tickSpacing", "stable", "getReserves", "owner", "name", "liquidity"]
    # per contract: the 10 views called on it, then balanceOf(it) called ON the RIZE token
    calls = []
    layout = []
    for a in contracts:
        calls += [(a, bytes.fromhex(SEL[nm])) for nm in names]
        calls.append((rize, call_address(SEL_BALANCE_OF, a)))
        layout.append(a)
    res = multicall(client, calls, group=120, log=log) if calls else []
    step = len(names) + 1
    info = {}
    for i, a in enumerate(layout):
        part = res[i * step:(i + 1) * step]
        d = dict(zip(names, part[:len(names)]))
        ok = lambda k: d[k][0] and len(d[k][1]) >= 32  # noqa: E731
        rec = {"balance": dec_uint(part[-1][1]) if part[-1][0] and len(part[-1][1]) >= 32 else None}
        if ok("token0") and ok("token1"):
            rec["token0"], rec["token1"] = dec_address(d["token0"][1]), dec_address(d["token1"][1])
        if ok("factory"):
            rec["factory"] = dec_address(d["factory"][1])
        if ok("fee"):
            rec["fee"] = dec_uint(d["fee"][1])
        if ok("tickSpacing"):
            rec["tick"] = dec_uint(d["tickSpacing"][1])
        if ok("stable"):
            rec["stable"] = bool(dec_uint(d["stable"][1]))
        if d["getReserves"][0] and len(d["getReserves"][1]) >= 64:
            rec["reserves"] = (dec_uint(d["getReserves"][1][:32]), dec_uint(d["getReserves"][1][32:64]))
        if ok("owner"):
            rec["owner"] = dec_address(d["owner"][1])
        if d["name"][0] and d["name"][1]:
            rec["name"] = dec_string(d["name"][1])
        if ok("liquidity"):
            rec["liquidity"] = dec_uint(d["liquidity"][1])
        info[a] = rec
    tok = set()
    for rec in info.values():
        tok.update(x for x in (rec.get("token0"), rec.get("token1")) if x)
    meta = read_token_meta(client, tok | {rize}, log) if tok else {rize: ("RIZE", 18)}
    labels = load_labels(conn, [a for a, _ in addrs])

    log("=" * 78)
    for a, n in addrs:
        lab = f"   label: {labels[a]}" if a in labels else ""
        head = f"{a}" + (f"   {n:,} transfers" if n else "")
        if not kinds.get(a):
            log(f"{head}\n    WALLET (no code){lab}")
            continue
        rec = info[a]
        bal = rec["balance"]
        bal_s = f"{Decimal(bal) / Decimal(10) ** 18:,.0f} RIZE" if bal is not None else "?"
        if "token0" in rec:
            def sym(t, _m=meta):
                name = _m.get(t, (t[:8], 18))[0]
                same = name.upper() == _m.get(rize, ("RIZE", 18))[0].upper()
                return f"{name}[NOT OUR RIZE: {t[:10]}…]" if same and t != rize else name
            style = ("concentrated-liquidity pool (tickSpacing %s)" % rec["tick"] if "tick" in rec else
                     "constant-product style pool (getReserves)" if "reserves" in rec else "pool-like contract")
            extra = []
            if "fee" in rec:
                extra.append(f"fee {rec['fee']}")
            if "stable" in rec:
                extra.append("stable curve" if rec["stable"] else "volatile curve")
            if "reserves" in rec:
                extra.append("reserves " + " / ".join(
                    f"{Decimal(x) / Decimal(10) ** meta.get(t, ('', 18))[1]:,.2f} {sym(t)}"
                    for x, t in zip(rec["reserves"], (rec["token0"], rec["token1"]))))
            log(f"{head}\n    POOL {sym(rec['token0'])}/{sym(rec['token1'])}  -  {style}  {' | '.join(extra)}{lab}")
            log(f"    factory {rec.get('factory', '(none)')}   holds {bal_s}   token0 {rec['token0']}   token1 {rec['token1']}")
        else:
            bits = [f"name {rec['name']!r}"] if rec.get("name") else []
            if rec.get("owner"):
                bits.append(f"owner {rec['owner']}")
            if "factory" in rec:
                bits.append(f"factory {rec['factory']}")
            log(f"{head}\n    CONTRACT (not a pool)  {'  '.join(bits)}  holds {bal_s}{lab}")
    log("=" * 78)
    factories = {}
    for a, rec in info.items():
        if "token0" in rec and rec.get("factory"):
            factories.setdefault(rec["factory"], []).append(a)
    if factories:
        log("Pools per factory (the factory address is what identifies the DEX):")
        for f, ps in sorted(factories.items(), key=lambda kv: -len(kv[1])):
            log(f"   {f}: {len(ps)} pool(s)")
    return True


# ------------------------------------------------------------------------------------------------- main
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="indexer.market_check", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["trade-check", "classify"])
    p.add_argument("--chain", default="base")
    p.add_argument("--txs", default="", help="comma-separated transaction hashes (trade-check)")
    p.add_argument("--recent", type=int, default=0,
                   help="trade-check: instead of --txs, take the N most recent transactions of this chain from the database")
    p.add_argument("--min-rize", type=int, default=0, help="with --recent: only transactions moving at least this many RIZE")
    p.add_argument("--top", type=int, default=40, help="busiest addresses to classify (classify)")
    p.add_argument("--addresses", default="", help="comma-separated addresses to classify in addition")
    p.add_argument("--full", action="store_true", help="trade-check: print the raw transfers and the address legend even for many transactions")
    args = p.parse_args(argv)

    def log(msg: str) -> None:
        print(msg, flush=True)

    chains = config.load_chains()
    if args.chain not in chains:
        log(f"unknown chain {args.chain!r}; choose one of {', '.join(chains)}")
        return 2
    chain = chains[args.chain]
    client = RpcClient(config.rpc_url(chain), batch_size=config.env_int("RPC_BATCH_SIZE", 3),
                       max_rps=float(config.env_int("RPC_MAX_RPS", 20)))
    client.timeout = 25.0
    client.max_retries = 3
    conn = None
    try:
        from . import db
        conn = db.connect(config.database_url())
    except (Exception, SystemExit) as e:
        log(f"(database not available: {type(e).__name__}; labels and top addresses skipped)")
    try:
        if args.mode == "trade-check":
            txs = [t.strip().lower() for t in args.txs.replace("\n", ",").split(",") if t.strip()]
            if args.recent:
                if conn is None:
                    log("--recent needs the database")
                    return 2
                txs = recent_txs(conn, chain.chain_id, args.recent, args.min_rize)
                log(f"{len(txs)} most recent transactions on {chain.name} (from the database):")
                for t in txs:
                    log("  " + t)
            txs = txs or [DEFAULT_TX]
            bad = [t for t in txs if not (t.startswith("0x") and len(t) == 66)]
            if bad:
                log(f"not a transaction hash: {bad[0]}")
                return 2
            ok = trade_check(client, chain, txs, conn, log, brief=(len(txs) > 3 and not args.full))
        else:
            extra = [a.strip().lower() for a in args.addresses.replace("\n", ",").split(",") if a.strip()]
            rows: list[tuple[str, int | None]] = []
            if conn is not None and args.top > 0:
                rows = [(a, n) for a, n in top_addresses(conn, chain.chain_id, args.top)]
            elif args.top > 0:
                log("no database: only the given addresses can be classified")
            have = {a for a, _ in rows}
            rows += [(a, None) for a in extra if a not in have]
            if not rows:
                log("nothing to classify")
                return 2
            ok = classify(client, chain, rows, conn, log)
        log("DONE. Paste this whole log back; nothing was written anywhere.")
        return 0 if ok else 1
    except RpcError as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        return 1
    except Exception as e:
        import traceback
        traceback.print_exc()
        log(f"CRASHED: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
