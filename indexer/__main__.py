"""Command line: python -m indexer <command> [--chain all|base|ethereum|bsc|polygon]"""

from __future__ import annotations

import argparse
import json
import os
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


def run_unified_command(args) -> int:
    """UNIFIED UPDATE EXPLORIZE: everything in one transaction, published when it is all at the same blocks (see indexer/unified.py).
    Anomalies never stop the publication: they are appended (once each) to the text file anomalies/ANOMALIES.txt."""
    import traceback
    from datetime import datetime, timezone
    from pathlib import Path

    from . import anomalies as anomalies_mod
    from .unified import RunFailed, record, run_unified

    if args.chain != "all":
        raise SystemExit("unified always updates every chain together: do not pass --chain")
    path = Path(os.environ.get("ANOMALIES_FILE", "anomalies/ANOMALIES.txt"))

    def write(items) -> None:
        try:
            n = anomalies_mod.append(path, items)
            if n:
                log(f"{n} new anomaly line(s) written to {path}")
        except Exception as e:  # noqa: BLE001 - the anomaly file must never break a run
            log(f"could not write {path}: {type(e).__name__}")

    day = f"{datetime.now(timezone.utc):%Y-%m-%d}"
    url = config.database_url()
    chains = config.select_chains("all")
    started = datetime.now(timezone.utc)
    setup = db.connect(url)                       # tables first, on their own connection: schema statements lock tables
    try:
        db.init_schema(setup)
    finally:
        setup.close()
    reg = Registry.from_dir()
    clients = {c.key: make_client(c) for c in chains}
    conn = db.connect_staged(url)
    budget = (args.max_minutes or 25) * 60
    found: list = []
    try:
        summary = run_unified(conn, chains, clients, reg, max_seconds=budget, log=log, anomalies=found)
        if summary.get("skipped"):
            conn.abort()
            log(f"SKIPPED: {summary['skipped']}")
            return 0
        conn.publish()
        log(f"PUBLISHED. {json.dumps(summary, default=str)}")
        write(found)
        return 0
    except RunFailed as e:
        conn.abort()
        log(f"NOT PUBLISHED (the site keeps the last good state): {e}")
        record(url, started, False, {"failed": True}, str(e)[:2000])
        # only the reason is written: the anomalies found by this run belong to data that was rolled back, the next run finds them again
        write([(f"blocked:{code}:{day}", "NOT PUBLISHED", str(e)[:300]) for code in (e.codes or ["other"])[:1]])
        return 1
    except BaseException as e:  # noqa: BLE001 - whatever happens, nothing half-done is published
        try:
            conn.abort()
        except Exception:  # noqa: BLE001
            pass
        traceback.print_exc()
        log(f"NOT PUBLISHED (the site keeps the last good state): {type(e).__name__}")
        try:
            record(url, started, False, {"crashed": True}, f"{type(e).__name__}"[:300])
        except Exception:  # noqa: BLE001
            pass
        # the repository is public: only the kind of error is written, never its text (it could carry a host name)
        write([(f"error:{type(e).__name__}:{day}", "NOT PUBLISHED", f"the run stopped on an error ({type(e).__name__}); see the workflow log")])
        return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="indexer", description=__doc__)
    p.add_argument("command", choices=["init-db", "probe", "find-start", "sync", "verify", "verify-balances", "inspect", "balance", "status",
                                          "enrich", "snapshot", "discover", "ccip", "ccip-gaps", "repair", "reprocess", "report", "tokens", "unified"])
    p.add_argument("--chain", default="all")
    p.add_argument("--max-minutes", type=float, default=None,
                   help="stop cleanly after this many minutes (progress is saved)")
    p.add_argument("--top", type=int, default=200, help="verify-balances: largest holders to check")
    p.add_argument("--sample", type=int, default=300, help="verify-balances: random other addresses")
    p.add_argument("--all", action="store_true", help="verify-balances: check every address (slow)")
    p.add_argument("--from-block", type=int, default=None, help="inspect: first block")
    p.add_argument("--to-block", type=int, default=None, help="inspect: last block")
    p.add_argument("--receipts", type=int, default=0, help="inspect: show the token logs of the first N transactions")
    p.add_argument("--address", default=None, help="balance: wallet address; reprocess: addresses (comma-separated)")
    p.add_argument("--blocks", default=None, help="balance: comma-separated block numbers")
    p.add_argument("--rebuild", action="store_true", help="enrich: forget the trades / liquidity moves of the chain and recompute them; tokens: ask the unreadable contracts again")
    p.add_argument("--limit", type=int, default=300, help="ccip: burn transactions to look up per chain in one run")
    p.add_argument("--discover-top", type=int, default=60, help="discover: how many of the busiest addresses to test")
    p.add_argument("--reasons", default=None, help="reprocess: review reasons to re-read (unknown_pool,guess,flag,crash; none = skip)")
    p.add_argument("--pool", default=None, help="reprocess: pool address or pool id (comma-separated)")
    p.add_argument("--flags", default=None, help="reprocess: trade flags to re-read (comma-separated, e.g. price-implausible)")
    p.add_argument("--tx", default=None, help="reprocess: transaction hashes (comma-separated, or 'chain hash' one per line)")
    p.add_argument("--tx-file", default=None, help="reprocess: text file with one transaction per line")
    p.add_argument("--lp-txs", action="store_true", help="reprocess: every transaction with liquidity moves stored by an older reading (resumable)")
    p.add_argument("--outliers", action="store_true", help="reprocess: trades of an older reading priced far from the median of their day (resumable)")
    p.add_argument("--dry-run", action="store_true", help="reprocess: only say what would be re-read")
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

    if args.command == "unified":
        return run_unified_command(args)

    conn = db.connect(config.database_url())
    budget = args.max_minutes * 60 if args.max_minutes else None
    t_start = time.monotonic()
    reg = None
    if args.command in ("enrich", "snapshot", "discover", "ccip", "ccip-gaps", "repair", "reprocess", "report", "tokens"):
        db.init_schema(conn)                                  # idempotent: creates the market tables on first use
        db.ensure_chains(conn, chains)
        reg = Registry.from_dir()
        enrich_mod.sync_registry(conn, reg, config.load_chains())
        log(f"registry: {len(reg.pools())} pools ({sum(1 for x in reg.pools() if x.source == 'auto')} found automatically)")
    if args.command == "report":
        enrich_mod.report(conn, log)
        return 0
    if args.command == "tokens":
        from . import tokens as tokens_mod
        clients = {c.key: make_client(c) for c in chains}
        res = tokens_mod.discover(conn, clients, {c.key: c for c in chains}, reg, log, retry_unreadable=args.rebuild)
        log(f"tokens: {res}")
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
    selection = None
    if args.command == "reprocess":
        from .reprocess import build_selection
        selection = build_selection(args.reasons, args.pool, args.address, args.flags, args.tx, args.tx_file,
                                      lp_txs=args.lp_txs, outliers=args.outliers)
        if selection.empty():
            raise SystemExit("reprocess: nothing selected (give --reasons, --pool, --address, --flags, --tx, --tx-file, --lp-txs or --outliers)")
        log(f"reprocess: reasons={list(selection.reasons)} pools={list(selection.pools)} addresses={list(selection.addresses)} "
            f"flags={list(selection.flags)} listed transactions={len(selection.txs)} lp_txs={selection.lp_txs} outliers={selection.outliers}"
            f"{' (dry run)' if args.dry_run else ''}")
    for c in chains:
        try:
            client = None if (args.command == "reprocess" and args.dry_run) else make_client(c)     # a dry run never calls the node
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
            elif args.command == "reprocess":
                from .reprocess import reprocess_chain
                left = None if budget is None else max(budget - (time.monotonic() - t_start), 1.0)
                reprocess_chain(c, client, conn, reg, selection, dry_run=args.dry_run, max_seconds=left, log=log)
            elif args.command == "snapshot":
                enrich_mod.snapshot(c, client, conn, reg, log=log)
            elif args.command == "discover":
                enrich_mod.discover(client, conn, c, reg, args.discover_top, log)
        except (RpcError, SystemExit, psycopg2.Error, DataError, RuntimeError) as e:
            # one failing chain must not block the others; the exit code reports it
            conn.rollback()
            log(f"[{c.key}] FAILED: {type(e).__name__}: {e}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
