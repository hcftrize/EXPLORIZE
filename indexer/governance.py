"""Governance diagnostic (read only): python -m indexer.governance [--max-id 11000]

Reads the whole T-RIZE bonding state on Base through Multicall3, at ONE pinned block, and checks the
assumptions the "Top Holders" job will rely on. It writes nothing to the database.

What it checks
  1. Multicall3 exists on the four chains (needed to read many balances in one call).
  2. Every bond id 1..max-id: owner (BondNFT), bonded, brokenBond (queued for release), vested (still locked)
     and the release queue.
  3. The key invariant:  RIZE balance of the governance contract  ==  sum(bonded) + sum(brokenBond).
  4. The release queue of each bond adds up to its brokenBond.
  5. Three witness wallets against what the daily governance JSON says.
  6. Our own transfers (database) against the contract: reconstructed contract balance, and how many bond
     owners never appear in a transfer (they will be added to the holder candidates).

Environment: DRPC_API_KEY (or RPC_URL_<CHAIN>), DATABASE_URL (optional, read only here).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from decimal import Decimal

from . import config
from .multicall import (MULTICALL3, SEL_BALANCE_OF, SEL_BONDED, SEL_BROKEN_BOND, SEL_GET_BOND_RELEASE,
                        SEL_GET_RELEASE_WARMUP, SEL_GET_SCHEDULE, SEL_GET_VESTING_INITIATOR, SEL_OWNER_OF,
                        SEL_TOTAL_SUPPLY, SEL_VESTED, call_address, call_none, call_uint, dec_address,
                        dec_release_list, dec_schedule, dec_uint, multicall)
from .rpc import RpcClient, RpcError

GOVERNANCE = "0x5a134098bDBEb05Da9eAc35439c5624547ed26eE"   # GovernanceBonding (Base)
BOND_NFT = "0xF33F231B769CCC1C7f74190De14340fBD51879FF"      # BondNFT (Base)
ONE = 10 ** 18

# What the daily governance JSON (bond-states.json, generated 2026-11-13 06:01 UTC) says.
JSON_TOTAL_RIZE = Decimal("887899974.76")
WITNESSES = {
    "0xf2f327e6c6df13950e40256ef8d0c83c87ee2e80": (5, Decimal("88938100.0")),
    "0x588ebb657ca52d6fbdf8f52c760d53c1474e37ed": (13, Decimal("6.5242")),
    "0x08f326ac9192d92584d33d1b2560c4460291643b": (3, Decimal("0.0")),
}


def fmt(raw: int | None) -> str:
    if raw is None:
        return "n/a"
    return f"{Decimal(raw) / ONE:,.4f}"


def when(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def read_bonds(client, block: str, max_id: int, log) -> dict[int, dict]:
    """One snapshot of every bond id 1..max_id (5 views per id)."""
    calls = []
    for i in range(1, max_id + 1):
        calls += [(BOND_NFT, call_uint(SEL_OWNER_OF, i)),
                  (GOVERNANCE, call_uint(SEL_BONDED, i)),
                  (GOVERNANCE, call_uint(SEL_BROKEN_BOND, i)),
                  (GOVERNANCE, call_uint(SEL_VESTED, i)),
                  (GOVERNANCE, call_uint(SEL_GET_BOND_RELEASE, i))]
    log(f"Reading {max_id} bond ids ({len(calls)} sub-calls) at block {int(block, 16)} ...")
    res = multicall(client, calls, block, group=250, log=log)
    bonds: dict[int, dict] = {}
    for n, i in enumerate(range(1, max_id + 1)):
        (ok_o, o), (ok_b, b), (ok_k, k), (ok_v, v), (ok_q, q) = res[5 * n:5 * n + 5]
        bonds[i] = {
            "exists": ok_o and len(o) >= 32,
            "owner": dec_address(o) if ok_o and len(o) >= 32 else None,
            "bonded": dec_uint(b) if ok_b and len(b) >= 32 else None,
            "broken": dec_uint(k) if ok_k and len(k) >= 32 else None,
            "vested": dec_uint(v) if ok_v and len(v) >= 32 else None,
            "queue": dec_release_list(q) if ok_q else None,
            "qraw": q.hex() if ok_q and len(q) > 64 else None,
        }
    return bonds


def run(clients: dict[str, RpcClient], chains: dict[str, config.Chain], max_id: int, conn, log) -> bool:
    ok = True

    log("=" * 70)
    log("1. Multicall3 on every chain")
    for key, c in chains.items():
        try:
            code = clients[key].get_code(MULTICALL3)
            present = isinstance(code, str) and len(code) > 4
            cid = clients[key].chain_id()
            log(f"  [{key}] chain id {cid} (expected {c.chain_id}); "
                f"Multicall3 {'PRESENT' if present else 'MISSING'} ({max(len(code) - 2, 0) // 2} bytes of code)")
            ok &= present and cid == c.chain_id
        except RpcError as e:
            log(f"  [{key}] FAILED: {e}")
            ok = False

    base = chains["base"]
    client = clients["base"]
    head = client.block_number()
    block = hex(head)
    snap_ts = client.get_block_timestamps([head])[head]
    log("=" * 70)
    log(f"2. Snapshot of the governance state: Base block {head} ({when(snap_ts)})")

    single = multicall(client, [
        (base.token, call_address(SEL_BALANCE_OF, GOVERNANCE)),
        (BOND_NFT, call_none(SEL_TOTAL_SUPPLY)),
        (GOVERNANCE, call_none(SEL_GET_RELEASE_WARMUP)),
    ], block)
    for label, (good, data) in zip(("RIZE balance of GovernanceBonding", "BondNFT totalSupply",
                                    "release warmup (seconds)"), single):
        val = dec_uint(data) if good and len(data) >= 32 else None
        shown = fmt(val) if label.startswith("RIZE") else (str(val) if val is not None else "n/a")
        log(f"  {label}: {shown}")
    contract_balance = dec_uint(single[0][1]) if single[0][0] else None
    nft_supply = dec_uint(single[1][1]) if single[1][0] else None

    bonds = read_bonds(client, block, max_id, log)
    live = {i: b for i, b in bonds.items() if b["exists"]}
    highest = max(live) if live else 0
    broken_reads = [i for i, b in live.items() if None in (b["bonded"], b["broken"], b["vested"], b["queue"])]
    log(f"  bonds with an owner: {len(live)}  (BondNFT totalSupply says {nft_supply})")
    log(f"  highest existing id: {highest}  (scan went up to {max_id})")
    if highest > max_id - 50:
        log("  WARNING: the scan limit is too close to the highest id. Re-run with a bigger --max-id.")
        ok = False
    log(f"  ids without owner (burned or never minted, inside the scan): {max_id - len(live)}")
    log(f"  existing bonds where a view call failed: {len(broken_reads)} {broken_reads[:10]}")
    ghost = [i for i, b in bonds.items() if not b["exists"] and (b["bonded"] or b["broken"])]
    log(f"  ids WITHOUT owner but with RIZE bonded/queued: {len(ghost)} {ghost[:10]}")

    s_bonded = sum(b["bonded"] or 0 for b in live.values())
    s_broken = sum(b["broken"] or 0 for b in live.values())
    s_vested = sum(b["vested"] or 0 for b in live.values())
    s_queue = sum(sum(a for a, _ in (b["queue"] or [])) for b in live.values())
    log("=" * 70)
    log("3. Totals and the key invariant")
    log(f"  sum bonded            : {fmt(s_bonded)}")
    log(f"  sum brokenBond (queue): {fmt(s_broken)}")
    log(f"  sum queue entries     : {fmt(s_queue)}   (must equal brokenBond)")
    log(f"  sum vested (locked)   : {fmt(s_vested)}")
    log(f"  bonded + brokenBond   : {fmt(s_bonded + s_broken)}")
    if contract_balance is not None:
        diff = contract_balance - (s_bonded + s_broken)
        log(f"  contract RIZE balance : {fmt(contract_balance)}")
        verdict = "OK (exact)" if diff == 0 else ("OK (tiny difference)" if abs(diff) < 1000 * ONE else "DIFFERENT")
        log(f"  difference (balance - (bonded+broken)): {fmt(diff)}   -> {verdict}")
        log(f"  for reference, the old reading 'balance - bonded' = {fmt(contract_balance - s_bonded)}")
    log(f"  daily JSON total bonded RIZE: {JSON_TOTAL_RIZE:,.2f}  vs contract bonded now: {fmt(s_bonded)}")
    mismatch = [i for i, b in live.items()
                if b["queue"] is not None and b["broken"] is not None
                and sum(a for a, _ in b["queue"]) != b["broken"]]
    log(f"  bonds whose queue does not add up to brokenBond: {len(mismatch)} {mismatch[:10]}")
    queued_bonds = [i for i, b in live.items() if b["queue"]]
    vested_bonds = [i for i, b in live.items() if b["vested"]]
    log(f"  bonds with a pending release: {len(queued_bonds)}   bonds still vesting: {len(vested_bonds)}")
    max_q = max((len(b["queue"]) for b in live.values() if b["queue"]), default=0)
    log(f"  longest release queue: {max_q} (the contract allows 16)")

    owners: dict[str, list[int]] = {}
    for i, b in live.items():
        owners.setdefault(b["owner"], []).append(i)
    log(f"  distinct bond owners: {len(owners)}   (daily JSON: 9944)")
    log("  top 10 owners by bonded + queued:")
    ranked = sorted(owners.items(), key=lambda kv: -sum((live[i]["bonded"] or 0) + (live[i]["broken"] or 0)
                                                          for i in kv[1]))
    for addr, ids in ranked[:10]:
        bd = sum(live[i]["bonded"] or 0 for i in ids)
        bk = sum(live[i]["broken"] or 0 for i in ids)
        vs = sum(live[i]["vested"] or 0 for i in ids)
        log(f"    {addr}  bonds={len(ids):>3}  bonded={fmt(bd):>22}  queued={fmt(bk):>20}  vested={fmt(vs):>20}")

    log("=" * 70)
    log("4. Witness wallets")
    detail_ids: list[int] = []
    for addr, (exp_n, exp_bonded) in WITNESSES.items():
        ids = sorted(owners.get(addr, []))
        bd = sum(live[i]["bonded"] or 0 for i in ids)
        bk = sum(live[i]["broken"] or 0 for i in ids)
        vs = sum(live[i]["vested"] or 0 for i in ids)
        log(f"  {addr}")
        log(f"    contract : {len(ids)} bonds, bonded {fmt(bd)}, queued for release {fmt(bk)}, vesting-locked {fmt(vs)}")
        log(f"    JSON     : {exp_n} bonds, bonded {exp_bonded:,.4f}   (the JSON is up to a day old)")
        for i in ids:
            b = live[i]
            q = "; ".join(f"{fmt(a)} at {when(t)} ({'READY' if t <= snap_ts else 'waiting'})"
                          for a, t in (b["queue"] or []))
            log(f"      bond #{i}: bonded {fmt(b['bonded'])}  queued {fmt(b['broken'])}  vested {fmt(b['vested'])}"
                + (f"  queue: {q}" if q else ""))
            if b["vested"]:
                detail_ids.append(i)
        raw = next((live[i]["qraw"] for i in ids if live[i]["qraw"]), None)
        if raw:
            log(f"    raw getBondRelease answer (first queued bond): 0x{raw[:260]}{'...' if len(raw) > 260 else ''}")

    log("=" * 70)
    log("5. Vesting details (schedule and initiator of a few vested bonds)")
    for i in (detail_ids + vested_bonds)[:5]:
        res = multicall(client, [(GOVERNANCE, call_uint(SEL_GET_SCHEDULE, i)),
                                 (GOVERNANCE, call_uint(SEL_GET_VESTING_INITIATOR, i))], block)
        (ok_s, s), (ok_i, ini) = res
        if ok_s and len(s) >= 128:
            sc = dec_schedule(s)
            log(f"  bond #{i}: owner {live[i]['owner']}  schedule amount {fmt(sc['amount'])}, cliff {sc['cliff']}s, "
                f"vesting {sc['vesting']}s, start {when(sc['start'])}, still locked {fmt(live[i]['vested'])}, "
                f"initiator {dec_address(ini) if ok_i else 'n/a'}")
        else:
            log(f"  bond #{i}: getSchedule failed")

    log("=" * 70)
    log("6. Our transfers against the contract")
    if conn is None:
        log("  (no database connection; skipped)")
    else:
        try:
            with conn.cursor() as cur:
                gov = bytes.fromhex(GOVERNANCE[2:].lower())
                cur.execute("SELECT COALESCE(SUM(CASE WHEN to_addr=%s THEN amount ELSE 0 END),0)"
                            " - COALESCE(SUM(CASE WHEN from_addr=%s THEN amount ELSE 0 END),0)"
                            " FROM transfers WHERE chain_id=%s AND (to_addr=%s OR from_addr=%s)",
                            (gov, gov, base.chain_id, gov, gov))
                rebuilt = int(cur.fetchone()[0])
                log(f"  contract balance rebuilt from our transfers: {fmt(rebuilt)}")
                if contract_balance is not None:
                    log(f"  live balance                                : {fmt(contract_balance)}")
                    log(f"  difference                                  : {fmt(contract_balance - rebuilt)}")
                bin_owners = [bytes.fromhex(a[2:]) for a in owners]
                cur.execute("SELECT count(*) FROM unnest(%s::bytea[]) AS a(addr)"
                            " WHERE NOT EXISTS (SELECT 1 FROM transfers t WHERE t.to_addr = a.addr)"
                            "   AND NOT EXISTS (SELECT 1 FROM transfers t WHERE t.from_addr = a.addr)",
                            (bin_owners,))
                missing = cur.fetchone()[0]
                log(f"  bond owners that never appear in any of our transfers: {missing} of {len(owners)}")
            conn.rollback()
        except Exception as e:  # diagnostic only: report and go on
            conn.rollback()
            log(f"  database check failed: {type(e).__name__}: {e}")

    log("=" * 70)
    log("DONE. Paste this whole log back; nothing was written anywhere.")
    return ok


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="indexer.governance", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-id", type=int, default=11000, help="highest bond id to scan (default 11000)")
    args = p.parse_args(argv)

    def log(msg: str) -> None:
        print(msg, flush=True)

    chains = config.load_chains()
    clients = {k: RpcClient(config.rpc_url(c), batch_size=config.env_int("RPC_BATCH_SIZE", 3),
                            max_rps=float(config.env_int("RPC_MAX_RPS", 20)))
               for k, c in chains.items()}
    for cl in clients.values():          # fail fast and visibly instead of waiting minutes on a stuck node
        cl.timeout = 25.0
        cl.max_retries = 3
    conn = None
    try:
        from . import db
        conn = db.connect(config.database_url())
    except (Exception, SystemExit) as e:  # the database part is optional here
        log(f"(database not available: {type(e).__name__}; section 6 will be skipped)")
    try:
        return 0 if run(clients, chains, args.max_id, conn, log) else 1
    except RpcError as e:
        log(f"FAILED: {type(e).__name__}: {e}")
        return 1
    except Exception as e:  # never end without saying why
        import traceback
        traceback.print_exc()
        log(f"CRASHED: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
