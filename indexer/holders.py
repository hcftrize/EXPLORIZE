"""RIZE Top Holders job: python -m indexer.holders

Once a day (or on demand) it rebuilds the ranking from the chains themselves, never from the daily
governance JSON:

  Held    live balanceOf(RIZE) of every known address, on every chain, summed
  Bonded  what the wallet still has inside the governance contract (Base): active bonds + RIZE queued for
          release (brokenBond), read bond by bond through the NFT owner
  Total   Held + Bonded            -> ranking, top 1000

Known addresses = every address that appears in our transfers (per chain) + every bond owner.
Held balances are read from the chain, so the negative balances our transfer history can show on BNB Chain
(CCIP burn / mint accounting) never reach the ranking.

The whole snapshot is written in ONE transaction (replacing the previous one), so the site never reads a
half-built ranking. If any read fails, nothing is written and the previous ranking stays.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone

import psycopg2.extras

from . import config, db
from .governance import BOND_NFT, GOVERNANCE, read_bonds
from .multicall import (SEL_BALANCE_OF, SEL_GET_SCHEDULE, SEL_GET_VESTING_INITIATOR, SEL_TOTAL_SUPPLY,
                        call_address, call_none, call_uint, dec_address, dec_schedule, dec_uint, multicall)
from .rpc import RpcClient, RpcError

TOP_N = 1000
ZERO = "0x" + "00" * 20
SCAN_MARGIN = 100          # ids scanned beyond BondNFT.totalSupply, to catch new bonds and gaps
EXTEND_STEP = 500
BALANCE_GROUP = 300


def fmt(raw: int) -> str:
    return f"{raw / 10 ** 18:,.2f}"


def read_all_bonds(client, block: str, log) -> tuple[dict[int, dict], int]:
    """Every bond that exists. Scans 1..totalSupply+margin and keeps going while new ids still turn up."""
    (ok, data), = multicall(client, [(BOND_NFT, call_none(SEL_TOTAL_SUPPLY))], block)
    if not ok or len(data) < 32:
        raise RpcError("BondNFT.totalSupply() failed")
    supply = dec_uint(data)
    last = supply + SCAN_MARGIN
    bonds = read_bonds(client, block, last, log)
    while any(bonds[i]["exists"] for i in range(max(1, last - SCAN_MARGIN + 1), last + 1)):
        first = last + 1
        last += EXTEND_STEP
        log(f"  bond ids still exist near {first - 1}; extending the scan to {last}")
        bonds.update(read_bonds(client, block, last, log, first=first))
    return {i: b for i, b in bonds.items() if b["exists"]}, supply


def read_vesting(client, block: str, bonds: dict[int, dict]) -> dict[int, dict]:
    """Schedule + initiator of every bond that is still locked by vesting."""
    ids = sorted(i for i, b in bonds.items() if b["vested"])
    calls = []
    for i in ids:
        calls += [(GOVERNANCE, call_uint(SEL_GET_SCHEDULE, i)), (GOVERNANCE, call_uint(SEL_GET_VESTING_INITIATOR, i))]
    res = multicall(client, calls, block, group=200)
    out = {}
    for n, i in enumerate(ids):
        (ok_s, s), (ok_i, ini) = res[2 * n], res[2 * n + 1]
        if not ok_s or len(s) < 128:
            raise RpcError(f"getSchedule failed for bond {i}")
        sc = dec_schedule(s)
        out[i] = {"amount": str(sc["amount"]), "cliff": sc["cliff"], "vesting": sc["vesting"],
                  "start": sc["start"], "initiator": dec_address(ini) if ok_i and len(ini) >= 32 else None}
    return out


def known_addresses(conn) -> dict[str, set[str]]:
    """chain key -> every address (0x lowercase) seen in our transfers, minus the zero address."""
    chains = config.load_chains()
    by_id = {c.chain_id: k for k, c in chains.items()}
    out: dict[str, set[str]] = {k: set() for k in chains}
    with conn.cursor() as cur:
        cur.execute("SELECT chain_id, addr FROM (SELECT chain_id, from_addr AS addr FROM transfers"
                    " UNION SELECT chain_id, to_addr FROM transfers) s WHERE addr <> %s",
                    (bytes(20),))
        for chain_id, addr in cur:
            key = by_id.get(chain_id)
            if key:
                out[key].add("0x" + bytes(addr).hex())
    conn.rollback()
    return out


def read_balances(client, token: str, block: str, addresses: list[str], log) -> dict[str, int]:
    calls = [(token, call_address(SEL_BALANCE_OF, a)) for a in addresses]
    res = multicall(client, calls, block, group=BALANCE_GROUP, log=log)
    out = {}
    for a, (ok, data) in zip(addresses, res):
        if not ok or len(data) < 32:
            raise RpcError(f"balanceOf failed for {a}")
        out[a] = dec_uint(data)
    return out


def build(clients: dict[str, RpcClient], chains: dict[str, config.Chain], conn, log, now=None) -> dict:
    """Read everything and return the snapshot (nothing is written here)."""
    t0 = time.monotonic()
    seen = known_addresses(conn)
    base_client = clients["base"]
    heads = {k: clients[k].block_number() for k in chains}
    blocks = {k: hex(h) for k, h in heads.items()}
    snap_ts = base_client.get_block_timestamps([heads["base"]])[heads["base"]]
    log(f"Snapshot: Base block {heads['base']} ({datetime.fromtimestamp(snap_ts, timezone.utc):%Y-%m-%d %H:%M} UTC)")

    # 1. governance state (Base)
    bonds, supply = read_all_bonds(base_client, blocks["base"], log)
    vesting = read_vesting(base_client, blocks["base"], bonds)
    (ok_b, bal), = multicall(base_client, [(chains["base"].token, call_address(SEL_BALANCE_OF, GOVERNANCE))],
                             blocks["base"])
    if not ok_b:
        raise RpcError("balanceOf(GovernanceBonding) failed")
    contract_balance = dec_uint(bal)

    active = {i: b for i, b in bonds.items() if (b["bonded"] or 0) > 0 or (b["broken"] or 0) > 0}
    if any(None in (b["bonded"], b["broken"], b["vested"], b["queue"]) for b in bonds.values()):
        raise RpcError("a bond could not be read completely")
    s_bonded = sum(b["bonded"] for b in active.values())
    s_queued = sum(b["broken"] for b in active.values())
    diff = contract_balance - s_bonded - s_queued
    log(f"Bonds: {len(bonds)} exist (totalSupply {supply}), {len(active)} still hold RIZE; "
        f"bonded {fmt(s_bonded)} + queued {fmt(s_queued)}; contract holds {fmt(contract_balance)}; "
        f"difference {fmt(diff)}" + ("" if diff == 0 else "   <-- DOES NOT ADD UP"))

    per_owner: dict[str, dict] = {}
    for i, b in active.items():
        o = per_owner.setdefault(b["owner"], {"bonded": 0, "queued": 0, "vested_locked": 0, "count": 0})
        o["bonded"] += b["bonded"]
        o["queued"] += b["broken"]
        o["vested_locked"] += b["vested"]
        o["count"] += 1

    # 2. live balances: every known address on its chains; bond owners are added on Base
    wanted = {k: set(v) for k, v in seen.items()}
    wanted["base"] |= set(per_owner)
    wanted["base"].discard(GOVERNANCE.lower())    # its balance is attributed to the bond owners
    held: dict[str, dict[str, int]] = {}
    n_read = 0
    for key, chain in chains.items():
        addrs = sorted(wanted[key])
        if not addrs:
            continue
        log(f"[{key}] reading {len(addrs)} balances at block {heads[key]} ...")
        for a, v in read_balances(clients[key], chain.token, blocks[key], addrs, log).items():
            n_read += 1
            if v:
                held.setdefault(a, {})[key] = v

    # 3. totals and ranking
    rows = []
    for a in set(held) | set(per_owner):
        h = held.get(a, {})
        o = per_owner.get(a, {"bonded": 0, "queued": 0, "vested_locked": 0, "count": 0})
        held_total = sum(h.values())
        total = held_total + o["bonded"] + o["queued"]
        if total > 0 and a != ZERO:
            rows.append({"address": a, "held": held_total, "held_by_chain": {k: str(v) for k, v in h.items()},
                         "bonded": o["bonded"], "queued": o["queued"], "vested_locked": o["vested_locked"],
                         "bond_count": o["count"], "total": total})
    rows.sort(key=lambda r: (-r["total"], r["address"]))
    top = rows[:TOP_N]
    for n, r in enumerate(top, 1):
        r["rank"] = n

    return {"top": top, "bonds": active, "vesting": vesting, "diff": diff, "contract_balance": contract_balance,
            "bonded_total": s_bonded, "queued_total": s_queued, "base_block": heads["base"],
            "snapshot_ts": snap_ts, "addresses_read": n_read, "holders_total": len(rows),
            "seconds": time.monotonic() - t0}


def store(conn, snap: dict) -> None:
    """Replace the previous snapshot with this one, in a single transaction."""
    b = lambda hexaddr: bytes.fromhex(hexaddr[2:])  # noqa: E731
    with conn.cursor() as cur:
        cur.execute("DELETE FROM holders")
        cur.execute("DELETE FROM bonds")
        psycopg2.extras.execute_values(cur, """
            INSERT INTO holders (rank, address, held, held_by_chain, bonded, queued, vested_locked, bond_count, total)
            VALUES %s""", [(r["rank"], b(r["address"]), r["held"], psycopg2.extras.Json(r["held_by_chain"]),
                            r["bonded"], r["queued"], r["vested_locked"], r["bond_count"], r["total"])
                           for r in snap["top"]])
        psycopg2.extras.execute_values(cur, """
            INSERT INTO bonds (nft_id, owner, bonded, queued, vested_locked, release_queue, vesting)
            VALUES %s""", [(i, b(x["owner"]), x["bonded"], x["broken"], x["vested"],
                            psycopg2.extras.Json([{"amount": str(a), "time": t} for a, t in x["queue"]]),
                            psycopg2.extras.Json(snap["vesting"][i]) if i in snap["vesting"] else None)
                           for i, x in sorted(snap["bonds"].items())])
        cur.execute("""
            INSERT INTO holders_meta (id, updated_at, base_block, snapshot_time, holders, addresses_read,
                                      bonds_active, contract_balance, bonded_total, queued_total,
                                      invariant_diff, duration_s)
            VALUES (1, now(), %s, to_timestamp(%s), %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET
              updated_at = EXCLUDED.updated_at, base_block = EXCLUDED.base_block,
              snapshot_time = EXCLUDED.snapshot_time, holders = EXCLUDED.holders,
              addresses_read = EXCLUDED.addresses_read, bonds_active = EXCLUDED.bonds_active,
              contract_balance = EXCLUDED.contract_balance, bonded_total = EXCLUDED.bonded_total,
              queued_total = EXCLUDED.queued_total, invariant_diff = EXCLUDED.invariant_diff,
              duration_s = EXCLUDED.duration_s""",
                    (snap["base_block"], snap["snapshot_ts"], len(snap["top"]), snap["addresses_read"],
                     len(snap["bonds"]), snap["contract_balance"], snap["bonded_total"], snap["queued_total"],
                     snap["diff"], round(snap["seconds"], 1)))
    conn.commit()


def run(clients, chains, conn, log, dry_run: bool = False) -> bool:
    db.init_schema(conn)                       # creates the three tables the first time (idempotent)
    snap = build(clients, chains, conn, log)
    log("Top 10:")
    for r in snap["top"][:10]:
        log(f"  #{r['rank']:<3} {r['address']}  total {fmt(r['total']):>18}  held {fmt(r['held']):>16}"
            f"  bonded {fmt(r['bonded']):>16}  queued {fmt(r['queued']):>14}  bonds {r['bond_count']}")
    log(f"{snap['holders_total']} wallets hold RIZE; keeping the top {len(snap['top'])}.")
    if dry_run:
        log("DRY RUN: nothing written.")
        return True
    store(conn, snap)
    log(f"Written. Snapshot of Base block {snap['base_block']}, {snap['seconds']:.0f}s of reading.")
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="indexer.holders", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="read and print the top 10, write nothing")
    args = p.parse_args(argv)

    def log(msg: str) -> None:
        print(msg, flush=True)

    chains = config.load_chains()
    clients = {k: RpcClient(config.rpc_url(c), batch_size=config.env_int("RPC_BATCH_SIZE", 3),
                            max_rps=float(config.env_int("RPC_MAX_RPS", 20)))
               for k, c in chains.items()}
    for cl in clients.values():
        cl.timeout = 25.0
        cl.max_retries = 3
    conn = db.connect(config.database_url())
    try:
        return 0 if run(clients, chains, conn, log, dry_run=args.dry_run) else 1
    except RpcError as e:
        conn.rollback()
        log(f"FAILED (nothing written, previous ranking kept): {type(e).__name__}: {e}")
        return 1
    except Exception as e:
        import traceback
        conn.rollback()
        traceback.print_exc()
        log(f"CRASHED (nothing written, previous ranking kept): {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
