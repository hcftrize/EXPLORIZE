"""Command line: python -m indexer <command> [--chain all|base|ethereum|bsc|polygon]"""

from __future__ import annotations

import argparse
import sys
import time

import psycopg2

from . import config, db
from .checks import probe_chain, probe_db, status_chain, verify_chain
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
    p.add_argument("command", choices=["init-db", "probe", "find-start", "sync", "verify", "status"])
    p.add_argument("--chain", default="all")
    p.add_argument("--max-minutes", type=float, default=None,
                   help="stop cleanly after this many minutes (progress is saved)")
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
            elif args.command == "status":
                status_chain(c, client, conn, log)
        except (RpcError, SystemExit, psycopg2.Error) as e:
            # one failing chain must not block the others; the exit code reports it
            conn.rollback()
            log(f"[{c.key}] FAILED: {type(e).__name__}: {e}")
            ok = False
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
