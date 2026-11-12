"""Diagnostics: probe (RPC + DB sanity and real node limits), verify (supply
check), status. Output never contains URLs, keys or connection strings."""

from __future__ import annotations

import time

from . import db
from .config import Chain
from .logs import TRANSFER_TOPIC0, ZERO_ADDRESS  # noqa: F401  (ZERO_ADDRESS re-exported)
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
    for width in (1_000, 10_000, 100_000, 1_000_000):
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
