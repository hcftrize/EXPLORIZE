"""Diagnostics: probe (RPC + DB sanity and real node limits), verify (supply
check), status. Output never contains URLs, keys or connection strings."""

from __future__ import annotations

import random
import time

from . import db
from .config import Chain
from .logs import TRANSFER_TOPIC0, ZERO_ADDRESS, decode_transfer  # noqa: F401
from .rpc import RangeTooLarge, RpcError


def _decode_string(hexdata: str) -> str:
    try:
        raw = bytes.fromhex(hexdata[2:])
        if len(raw) >= 64:  # ABI-encoded dynamic string
            n = int.from_bytes(raw[32:64], "big")
            return raw[64:64 + n].decode("utf-8", "replace")
        return raw.rstrip(b"\x00").decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        return "?"


def probe_chain(chain: Chain, client, log=print) -> bool:
    ok = True
    log(f"\n=== {chain.name} ({chain.key}) ===")
    try:
        cid = client.chain_id()
        head = client.block_number()
    except (RpcError, SystemExit) as e:
        log(f"  RPC unreachable: {e}")
        return False
    log(f"  chain id      : {cid} ({'OK' if cid == chain.chain_id else 'MISMATCH, expected %d' % chain.chain_id})")
    ok &= cid == chain.chain_id
    log(f"  head block    : {head:,}")

    def call_str(sel: str) -> str:
        return _decode_string(client.eth_call(chain.token, sel))

    try:
        name = call_str("0x06fdde03")
        symbol = call_str("0x95d89b41")
        decimals = int(client.eth_call(chain.token, "0x313ce567"), 16)
        supply = int(client.eth_call(chain.token, "0x18160ddd"), 16)
        log(f"  token         : {name} / {symbol}, decimals {decimals}, "
            f"totalSupply {supply / 10 ** decimals:,.2f}")
    except RpcError as e:
        log(f"  token calls failed: {e}  (wrong address for this chain?)")
        ok = False

    # Real eth_getLogs limits on this node/plan.
    end = max(head - 100, 1)
    sample = None
    for width in (1, 100, 1_000, 10_000, 100_000, 1_000_000):
        lo = max(end - width + 1, 0)
        t = time.monotonic()
        try:
            logs = client.get_logs(chain.token, [TRANSFER_TOPIC0], lo, end)
            log(f"  getLogs {width:>9,} blocks: OK, {len(logs):>6,} logs, {time.monotonic() - t:5.1f}s")
            if logs and sample is None:
                sample = logs[0]
        except RangeTooLarge as e:
            log(f"  getLogs {width:>9,} blocks: REFUSED ({e})")
        except RpcError as e:
            log(f"  getLogs {width:>9,} blocks: ERROR ({e})")
    if sample is not None:
        log(f"  logs carry blockTimestamp: {'yes' if sample.get('blockTimestamp') else 'no (block headers will be fetched)'}")

    # Archive support: state at an old block.
    old = max(head - 1_000_000, 1)
    try:
        client.get_code(chain.token, old)
        log(f"  archive state : yes (eth_getCode at block {old:,} worked)")
    except RpcError as e:
        log(f"  archive state : NO ({e}); find-start needs a paid archive plan")
    return ok


def probe_db(conn, log=print) -> bool:
    log("\n=== Database ===")
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        log(f"  {cur.fetchone()[0].split(',')[0]}")
        cur.execute("""SELECT table_name FROM information_schema.tables
                       WHERE table_schema = 'public' ORDER BY 1""")
        tables = [r[0] for r in cur.fetchall()]
    conn.commit()
    log(f"  tables        : {', '.join(tables) if tables else '(none yet: run init-db)'}")
    return True


def verify_chain(chain: Chain, client, conn, log=print) -> bool:
    """Supply rebuilt from our transfers must equal totalSupply() on-chain."""
    _, last = db.get_state(conn, chain.chain_id)
    if last is None:
        log(f"[{chain.key}] nothing indexed yet")
        return False
    ours = db.supply_from_transfers(conn, chain.chain_id, last)
    note = ""
    try:
        theirs = int(client.eth_call(chain.token, "0x18160ddd", hex(last)), 16)
    except RpcError:
        theirs = int(client.eth_call(chain.token, "0x18160ddd"), 16)
        note = " (compared with latest: only exact if no transfer happened since the checkpoint)"
    match = ours == theirs
    log(f"[{chain.key}] checkpoint block {last:,}: indexed supply {ours} vs on-chain {theirs} -> "
        f"{'OK' if match else 'MISMATCH'}{note}")
    return match


def status_chain(chain: Chain, client, conn, log=print) -> None:
    start, last = db.get_state(conn, chain.chain_id)
    s = db.chain_stats(conn, chain.chain_id)
    head = None
    try:
        head = client.block_number() if client else None
    except RpcError:
        pass
    lag = f", {head - last:,} blocks behind head" if (head and last is not None) else ""
    log(f"[{chain.key}] start {start}, checkpoint {last}{lag}; {s['rows']:,} transfers "
        f"({s['first']} -> {s['last']})")


def verify_balances(chain: Chain, client, conn, *, top: int = 200, sample: int = 300,
                    check_all: bool = False, log=print) -> bool:
    """Strongest completeness check: for a set of addresses, the balance rebuilt
    from our transfers must equal balanceOf() read on-chain at the same block.
    A single missing transfer makes at least one balance wrong."""
    _, last = db.get_state(conn, chain.chain_id)
    if last is None:
        log(f"[{chain.key}] nothing indexed yet")
        return False
    block = max(last - chain.reorg_margin, 0)  # stay clear of blocks that may still reorg
    mine = db.balances_up_to(conn, chain.chain_id, block)
    addrs = list(mine)
    if check_all:
        chosen = addrs
    else:
        by_balance = sorted(addrs, key=lambda a: mine[a], reverse=True)
        chosen = by_balance[:top]
        rest = by_balance[top:]
        chosen += random.Random(1).sample(rest, min(sample, len(rest)))  # includes zero balances
    log(f"[{chain.key}] checking {len(chosen):,} of {len(addrs):,} addresses at block {block:,}")

    calls = [("eth_call", [{"to": chain.token, "data": "0x70a08231" + a.hex().rjust(64, "0")}, hex(block)])
             for a in chosen]
    results = client.batch(calls)
    bad = []
    for a, r in zip(chosen, results):
        onchain = int(r, 16)
        if onchain != mine[a]:
            bad.append((a, mine[a], onchain))
    if bad:
        log(f"[{chain.key}] MISMATCH on {len(bad)} of {len(chosen)} addresses (first 10):")
        for a, ours, theirs in bad[:10]:
            log(f"    0x{a.hex()}  ours {ours / 10 ** 18:,.6f}  on-chain {theirs / 10 ** 18:,.6f}")
        return False
    log(f"[{chain.key}] balances OK: {len(chosen):,} addresses match on-chain at block {block:,}")
    return True


def inspect_range(chain: Chain, client, conn, lo: int, hi: int, log=print) -> bool:
    """Diagnostic: read the same blocks with windows of different widths, and compare
    with what we stored. Shows exactly which events the node returns, in which
    window, and which ones our database lacks (or has in excess)."""
    def read(a: int, b: int) -> list[dict]:
        raw = client.get_logs(chain.token, [TRANSFER_TOPIC0], a, b)
        return [d for d in (decode_transfer(l) for l in raw) if d]

    def key(d: dict) -> tuple:
        return (d["tx_hash"], d["log_index"])

    log(f"[{chain.key}] inspecting blocks {lo:,}-{hi:,}")
    seen: dict[int, dict] = {}
    events: dict[tuple, dict] = {}
    for ctx in (0, 1_000, 100_000):
        try:
            ds = read(max(lo - ctx, 0), hi + ctx)
        except RpcError as e:
            log(f"  window +/-{ctx:>7,}: refused ({e})")
            continue
        inside = {key(d): d for d in ds if lo <= d["block_number"] <= hi}
        seen[ctx] = inside
        events.update(inside)
        log(f"  window +/-{ctx:>7,}: {len(ds):>6,} events in the window, {len(inside)} inside the target blocks")

    with conn.cursor() as cur:
        cur.execute("SELECT tx_hash, log_index FROM transfers WHERE chain_id = %s AND block_number BETWEEN %s AND %s",
                    (chain.chain_id, lo, hi))
        in_db = {(bytes(t), i) for t, i in cur.fetchall()}
    conn.commit()

    consistent = all(set(v) == set(events) for v in seen.values())
    missing = set(events) - in_db
    extra = in_db - set(events)
    log(f"  node answers identical for every window width: {'yes' if consistent else 'NO (the node is inconsistent)'}")
    log(f"  events seen by the node: {len(events)}; in our database: {len(in_db)}; "
        f"missing from our database: {len(missing)}; in our database but not seen now: {len(extra)}")
    for k, d in sorted(events.items(), key=lambda kv: (kv[1]["block_number"], kv[1]["log_index"]))[:60]:
        widths = ",".join(str(c) for c, v in seen.items() if k in v) or "-"
        log(f"    block {d['block_number']:,} idx {d['log_index']:>5}  0x{d['from'].hex()[:8]}.. -> 0x{d['to'].hex()[:8]}.."
            f"  {d['amount'] / 10 ** 18:>16,.6f}  ts={d['block_ts']}  "
            f"{'IN-DB' if k in in_db else 'MISSING-FROM-DB'}  seen in windows: {widths}")
    return consistent and not missing and not extra
