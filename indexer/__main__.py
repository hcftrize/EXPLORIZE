"""Command line: python -m indexer <command> [--chain all|base|ethereum|bsc|polygon]"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg2

from . import config, db, enrich as enrich_mod
from .ccip import bridge_report, ccip_sync
from .registry import Registry
from .db import DataError
from .checks import balance_at, inspect_range, probe_chain, probe_db, status_chain, verify_balances, verify_chain
from .rpc import RpcClient, RpcError
from .sync import find_start_block, sync_chain


def make_client(chain: config.Chain) -> RpcClient:
    return RpcClient(
        config.rpc_url(chain),
        batch_size=config.env_int("RPC_BATCH_SIZE", 3),
        max_rps=float(config.env_int("RPC_MAX_RPS", 20)),
    )


def log(msg: str) -> None:
    print(msg, flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="indexer", description=__doc__)
    p.add_argument("command", choices=["init-db", "probe", "find-start", "sync", "verify", "verify-balances", "inspect", "balance", "status",
                                          "enrich", "snapshot", "discover", "ccip", "ccip-gaps", "repair", "report"])
    p.add_argument("--chain", default="all")
    p.add_argument("--max-minutes", type=float, default=None,
                   help="stop cleanly after this many minutes (progress is saved)")
    p.add_argument("--top", type=int, default=200, help="verify-balances: largest holders to check")
    p.add_argument("--sample", type=int, default=300, help="verify-balances: random other addresses")
    p.add_argument("--all", action="store_true", help="verify-balances: check every address (slow)")
    p.add_argument("--from-block", type=int, default=None, help="inspect: first block")
    p.add_argument("--to-block", type=int, default=None, help="inspect: last block")
    p.add_argument("--receipts", type=int, default=0, help="inspect: show the token logs of the first N transactions")
    p.add_argument("--address", default=None, help="balance: wallet address")
    p.add_argument("--blocks", default=None, help="balance: comma-separated block numbers")
    p.add_argument("--rebuild", action="store_true", help="enrich: forget the trades / liquidity moves of the chain and recompute them")
    p.add_argument("--limit", type=int, default=300, help="ccip: burn transactions to look up per chain in one run")
    p.add_argument("--discover-top", type=int, default=60, help="discover: how many of the busiest addresses to test")
    args = p.parse_args(argv)
    chains = config.select_chains(args.chain)
    ok = True

    if args.command == "init-db":
        conn = db.connect(config.database_url())
        db.init_schema(conn)
        db.ensure_chains(conn, chains)
        log("Schema is up to date.")
        return 0

    if args.command == "probe":
        for c in chains:
            ok &= probe_chain(c, make_client(c), log)
        conn = db.connect(config.database_url())
        ok &= probe_db(conn, log)
        return 0 if ok else 1

    conn = db.connect(config.database_url())
    budget = args.max_minutes * 60 if args.max_minutes else None
    t_start = time.monotonic()
    reg = None
    if args.command in ("enrich", "snapshot", "discover", "ccip", "ccip-gaps", "repair", "report"):
        db.init_schema(conn)                                  # idempotent: creates the market tables on first use
        db.ensure_chains(conn, chains)
        reg = Registry.from_dir()
        enrich_mod.sync_registry(conn, reg, config.load_chains())
        log(f"registry: {len(reg.pools())} pools ({sum(1 for x in reg.pools() if x.source == 'auto')} found automatically)")
    if args.command == "report":
        enrich_mod.report(conn, log)
        return 0
    if args.command == "ccip-gaps":
        from .ccip import gap_check
        clients = {c.chain_id: make_client(c) for c in chains}
        gap_check(conn, clients, reg, limit=args.limit, log=log)
        return 0
    if args.command == "ccip":
        ccip_sync(conn, chains, reg, limit=args.limit, max_seconds=budget, log=log)
        log(f"bridge report: {bridge_report(conn)}")
        return 0
    for c in chains:
        try:
            client = make_client(c)
            if args.command == "find-start":
                db.ensure_chains(conn, [c])
                db.set_start_block(conn, c.chain_id, find_start_block(client, c, client.block_number(), log))
            elif args.command == "sync":
                left = None if budget is None else max(budget - (time.monotonic() - t_start), 1.0)
                res = sync_chain(c, client, conn, max_seconds=left, log=log)
                log(f"[{c.key}] done: {res}")
            elif args.command == "verify":
                ok &= verify_chain(c, client, conn, log)
            elif args.command == "verify-balances":
                ok &= verify_balances(c, client, conn, top=args.top, sample=args.sample,
                                      check_all=args.all, log=log)
            elif args.command == "inspect":
                if args.from_block is None:
                    raise SystemExit("inspect needs --from-block (and optionally --to-block)")
                ok &= inspect_range(c, client, conn, args.from_block,
                                    args.to_block if args.to_block is not None else args.from_block,
                                    log=log, receipts=args.receipts)
            elif args.command == "balance":
                if not args.address or not args.blocks:
                    raise SystemExit("balance needs --address and --blocks (comma-separated)")
                ok &= balance_at(c, client, args.address,
                                 [int(x) for x in args.blocks.replace(" ", "").split(",") if x], log=log)
            elif args.command == "status":
                status_chain(c, client, conn, log)
            elif args.command == "enrich":
                if args.rebuild:
                    enrich_mod.rebuild(conn, c.chain_id)
                    log(f"[{c.key}] trades, liquidity moves and checkpoint cleared; recomputing")
                left = None if budget is None else max(budget - (time.monotonic() - t_start), 1.0)
                enrich_mod.enrich_chain(c, client, conn, reg, max_seconds=left, log=log)
            elif args.command == "repair":
                left = None if budget is None else max(budget - (time.monotonic() - t_start), 1.0)
                enrich_mod.repair_traders(c, client, conn, reg, max_seconds=left, log=log)
            elif args.command == "snapshot":
                enrich_mod.snapshot(c, client, conn, reg, log=log)
            elif args.command == "discover":
                enrich_mod.discover(client, conn, c, reg, args.discover_top, log)
        except (RpcError, SystemExit, psycopg2.Error, DataError) as e:
            # one failing chain must not block the others; the exit code reports it
            conn.rollback()
            log(f"[{c.key}] FAILED: {type(e).__name__}: {e}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
