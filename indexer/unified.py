"""UNIFIED UPDATE EXPLORIZE: one run updates everything the dashboard shows, all at the SAME block / timestamp.

    python -m indexer unified [--max-minutes 25]

Everything happens inside ONE database transaction (db.StagedConnection). The site reads the database through other connections, so
it keeps seeing the previous state until the very last statement of a successful run (COMMIT).

What BLOCKS the publication (alignment only: no tab may run ahead of, or behind, another)
  - a chain is not synced to its head, or the sync was cut by the time budget
  - trades / liquidity moves are not computed up to the same block as the transfers
  - CCIP: a burn or mint old enough to have a message was not looked up yet (in-flight messages, or transactions beyond the synced
    block, are NOT a problem)
  - the balances (and, when it is rebuilt, the top holders) are not written for the same blocks (balances are read at exactly the synced block of each chain)
  - an RPC error, a crash, a kill: nothing half-done is ever published

What only WRITES AN ANOMALY (anomalies/ANOMALIES.txt, each one once, grouped by day) and the data is published anyway
  - supply rebuilt from our transfers != totalSupply() on chain (per chain)
  - the balances of a chain do not add up to its totalSupply()
  - CCIP messages that do not reconcile (mismatch, one side missing, ...)
  - transactions the market layer could not read (crash) and governance-contract differences

Order of the run
  1. transfers      every chain is synced to its head (the same code as the `sync` command)
  2. supply check   supply rebuilt from our transfers vs totalSupply() on chain, per chain, at the synced block   (anomaly)
  3. market layer   trades and liquidity moves for the new transactions, crashed transactions retried, pool photos (hourly)
  4. CCIP           new burns / mints / in-flight messages asked to the Chainlink API, then every message reconciled
  5. balances       live balanceOf() at the synced block of every address touched since the last run (kept in holder_balances,
                    not displayed: cheap, and it keeps the "balances add up to totalSupply" check running at every run)
  6. top holders    ONCE A DAY (RANKING_EVERY_HOURS, 24 by default; always on the very first run): governance read + ranking =
                    balances + bonds (Base), written to holders / bonds for the same Base block as everything else. The other runs
                    keep the published table untouched, with its own date ("updated Xh ago")
  7. checks         alignment of every table, then the run is recorded and published

Not in this run: the very first backfill of a chain, and a long catch-up after days offline. Those keep their own commands
(sync / enrich / ccip / holders), which commit as they go; start this run once they are up to date.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

from . import config, db, enrich as enrich_mod, entities as entities_mod, holders as holders_mod, tokens as tokens_mod
from .ccip import bridge_report, ccip_backlog, ccip_sync
from .multicall import SEL_BALANCE_OF, call_address, dec_uint, multicall
from .registry import Registry
from .sync import sync_chain

ZERO = bytes(20)
ADVISORY_KEY = 7_390_001           # one unified run at a time
CCIP_GRACE_MINUTES = 15            # a burn / mint without CCIP message yet is asked again for this long before it is written off
SUPPLY_SELECTOR = "0x18160ddd"
# The Top Holders table (held + bonded + governance) is rebuilt at most this often. Between two rebuilds the page keeps the previous
# table, with its own date and block ("updated Xh ago"). Override with the RANKING_EVERY_HOURS environment variable (0 = every run).
RANKING_EVERY_HOURS = 24.0


MAX_ANOMALIES_PER_KIND = 500


class RunFailed(Exception):
    """The data cannot be aligned to one block / timestamp: the run must not be published. `codes` = stable names of the causes."""

    def __init__(self, message: str, codes: list[str] | None = None):
        super().__init__(message)
        self.codes = codes or []


def _ts(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# ------------------------------------------------------------------------------------------------ balances
def touched_addresses(conn, chain_id: int, lo: int | None, hi: int) -> set[bytes]:
    """Addresses that sent or received RIZE in blocks (lo, hi]; lo None = since the beginning."""
    with conn.cursor() as cur:
        if lo is None:
            cur.execute("SELECT from_addr FROM transfers WHERE chain_id = %s AND block_number <= %s "
                        "UNION SELECT to_addr FROM transfers WHERE chain_id = %s AND block_number <= %s",
                        (chain_id, hi, chain_id, hi))
        else:
            cur.execute("SELECT from_addr FROM transfers WHERE chain_id = %s AND block_number > %s AND block_number <= %s "
                        "UNION SELECT to_addr FROM transfers WHERE chain_id = %s AND block_number > %s AND block_number <= %s",
                        (chain_id, lo, hi, chain_id, lo, hi))
        out = {bytes(r[0]) for r in cur.fetchall()}
    conn.commit()
    return out


def update_balances(conn, clients, chains: dict[str, config.Chain], targets: dict[str, dict], log,
                    extra: dict[str, set[bytes]] | None = None) -> dict:
    """Bring holder_balances to EXACTLY the synced block of every chain, reading only what may have changed.

    Re-read: every address that sent / received RIZE since (previous balances block - reorg margin), plus `extra` = the addresses of
    the transfers that sync_chain was about to rescan and replace (a reorg can make an address disappear from the window)."""
    out = {}
    for key, chain in chains.items():
        client = clients[key]
        s_block = targets[key]["block"]
        with conn.cursor() as cur:
            cur.execute("SELECT balances_block FROM chain_state WHERE chain_id = %s", (chain.chain_id,))
            row = cur.fetchone()
        conn.commit()
        prev = row[0] if row else None
        if prev is None:
            addrs = touched_addresses(conn, chain.chain_id, None, s_block)
            seeding = True
        else:
            addrs = touched_addresses(conn, chain.chain_id, max(min(prev, s_block) - chain.reorg_margin, 0), s_block)
            addrs |= (extra or {}).get(key, set())
            seeding = False
        addrs.add(ZERO)                                           # its balance is part of the supply check
        hexes = sorted("0x" + a.hex() for a in addrs)
        log(f"[{key}] balances: reading {len(hexes):,} addresses at block {s_block:,}" + (" (first fill)" if seeding else ""))
        read = holders_mod.read_balances(client, chain.token, hex(s_block), hexes, log) if hexes else {}
        with conn.cursor() as cur:
            if seeding:
                cur.execute("DELETE FROM holder_balances WHERE chain_id = %s", (chain.chain_id,))
            else:
                cur.execute("DELETE FROM holder_balances WHERE chain_id = %s AND addr = ANY(%s::bytea[])",
                            (chain.chain_id, [psycopg2.Binary(bytes.fromhex(a[2:])) for a in hexes]))
            rows = [(chain.chain_id, psycopg2.Binary(bytes.fromhex(a[2:])), v) for a, v in read.items() if v > 0]
            if rows:
                psycopg2.extras.execute_values(cur, "INSERT INTO holder_balances (chain_id, addr, balance) VALUES %s", rows,
                                               page_size=2000)
            cur.execute("UPDATE chain_state SET balances_block = %s WHERE chain_id = %s", (s_block, chain.chain_id))
            cur.execute("SELECT COALESCE(sum(balance), 0), count(*) FROM holder_balances WHERE chain_id = %s", (chain.chain_id,))
            total, n = cur.fetchone()
        conn.commit()
        theirs = int(client.eth_call(chain.token, SUPPLY_SELECTOR, hex(s_block)), 16)
        out[key] = {"block": s_block, "read": len(hexes), "holders": n, "sum": int(total), "supply": theirs,
                    "seeded": seeding, "ok": int(total) == theirs}
        log(f"[{key}] balances add up to {int(total)} vs totalSupply {theirs} -> {'OK' if int(total) == theirs else 'MISMATCH (anomaly)'}")
    return out


def held_by_address(conn, chains: dict[str, config.Chain]) -> dict[str, dict[str, int]]:
    by_id = {c.chain_id: k for k, c in chains.items()}
    held: dict[str, dict[str, int]] = {}
    with conn.cursor() as cur:
        cur.execute("SELECT chain_id, addr, balance FROM holder_balances")
        for cid, addr, bal in cur:
            key = by_id.get(cid)
            if key:
                held.setdefault("0x" + bytes(addr).hex(), {})[key] = int(bal)
    conn.commit()
    return held


def update_ranking(conn, clients, chains: dict[str, config.Chain], balances: dict, log) -> dict:
    """Top holders and governance bonds at the Base balance block, from holder_balances + the governance contract."""
    base_block = balances["base"]["block"]
    base_client = clients["base"]
    gov = holders_mod.read_governance(base_client, hex(base_block), chains["base"].token, log)
    held = held_by_address(conn, chains)
    held.pop("0x" + ZERO.hex(), None)
    gov_addr = holders_mod.GOVERNANCE.lower()
    if gov_addr in held:                                          # its balance is attributed to the bond owners
        held[gov_addr].pop("base", None)
        if not held[gov_addr]:
            del held[gov_addr]
    top, total_holders = holders_mod.rank_holders(held, gov["per_owner"])
    ent_rows, ent_members = entities_mod.compute(entities_mod.load_groups(conn, chains), held, gov["per_owner"], list(chains))
    snap_ts = base_client.get_block_timestamps([base_block])[base_block]
    snap = {"entities": ent_rows, "entity_members": ent_members, "chains": chains, "top": top, "bonds": gov["bonds"], "vesting": gov["vesting"], "diff": gov["diff"],
            "contract_balance": gov["contract_balance"], "bonded_total": gov["bonded_total"],
            "queued_total": gov["queued_total"], "base_block": base_block, "snapshot_ts": snap_ts,
            "addresses_read": sum(b["read"] for b in balances.values()), "holders_total": total_holders, "seconds": 0.0}
    holders_mod.store(conn, snap)
    log(f"ranking: {total_holders:,} wallets hold RIZE, top {len(top)} stored (Base block {base_block:,}); "
        f"governance difference {gov['diff']}")
    return {"entities": len(ent_rows), "wallets": total_holders, "top": len(top), "base_block": base_block, "governance_diff": gov["diff"],
            "bonds": len(gov["bonds"])}


# ------------------------------------------------------------------------------------------------ checks
def structural_checks(conn, chains: dict[str, config.Chain], targets: dict, balances: dict, started, ranking_done: bool = True) -> list[str]:
    """What must agree across tabs. Returns the list of problems (empty = consistent)."""
    problems = []
    with conn.cursor() as cur:
        for key, chain in chains.items():
            t = targets[key]["block"]
            cur.execute("SELECT last_block, balances_block FROM chain_state WHERE chain_id = %s", (chain.chain_id,))
            last, bal_block = cur.fetchone()
            cur.execute("SELECT last_block FROM enrich_state WHERE chain_id = %s", (chain.chain_id,))
            e = cur.fetchone()
            if last != t:
                problems.append(f"{key}: transfers checkpoint {last} is not the block we validated ({t})")
            if e is None or e[0] is None or e[0] < t:
                problems.append(f"{key}: trades / liquidity moves are at block {e[0] if e else None}, transfers at {t}")
            if bal_block != balances[key]["block"]:
                problems.append(f"{key}: balances checkpoint {bal_block} differs from the block we validated")
            cur.execute("SELECT count(*) FROM trades x WHERE x.chain_id = %s AND x.block_number > %s", (chain.chain_id, t))
            if cur.fetchone()[0]:
                problems.append(f"{key}: trades exist beyond the synced block {t}")
        if ranking_done:
            cur.execute("SELECT base_block FROM holders_meta WHERE id = 1")
            m = cur.fetchone()
            if m is None or m[0] != balances["base"]["block"]:
                problems.append("top holders were not written for the validated Base block")
    conn.commit()
    return problems


def ranking_age_hours(conn, now: datetime) -> float | None:
    """Age of the published Top Holders table in hours (None = no table yet)."""
    with conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM holders_meta WHERE id = 1")
        row = cur.fetchone()
    conn.commit()
    return (now - row[0]).total_seconds() / 3600 if row else None


def ranking_due(conn, chains: dict[str, config.Chain], now: datetime, every_hours: float) -> tuple[bool, str]:
    """Is it time to rebuild the Top Holders table (governance read + ranking)? Yes when there is none, when the balances were never
    seeded (first run), or when the published one is older than `every_hours`."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM chain_state WHERE chain_id = ANY(%s) AND balances_block IS NULL",
                    ([c.chain_id for c in chains.values()],))
        unseeded = cur.fetchone()[0]
    conn.commit()
    age = ranking_age_hours(conn, now)
    if age is None:
        return True, "no Top Holders table yet"
    if unseeded:
        return True, "first run: balances are read for the first time"
    if age >= every_hours:
        return True, f"the published table is {age:.1f} h old (limit {every_hours:g} h)"
    return False, f"the published table is {age:.1f} h old (rebuilt every {every_hours:g} h)"


def last_ok_start(conn) -> datetime | None:
    with conn.cursor() as cur:
        cur.execute("SELECT started_at FROM unified_runs WHERE ok ORDER BY started_at DESC LIMIT 1")
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else None


def record(url_or_conn, started: datetime, ok: bool, summary: dict, note: str | None) -> None:
    """Journal of the runs. A successful run writes its line inside the transaction; a failed one, from a separate connection."""
    sql = "INSERT INTO unified_runs (started_at, ok, summary, note) VALUES (%s, %s, %s, %s)"
    args = (started, ok, psycopg2.extras.Json(summary, dumps=lambda o: json.dumps(o, default=str)), note)
    if isinstance(url_or_conn, str):
        raw = psycopg2.connect(url_or_conn, connect_timeout=20, application_name="explorize-unified-log")
        try:
            raw.autocommit = True
            with raw.cursor() as cur:
                cur.execute(sql, args)
        finally:
            raw.close()
    else:
        with url_or_conn.cursor() as cur:
            cur.execute(sql, args)
        url_or_conn.commit()


# ------------------------------------------------------------------------------------------------ the run
def _amount(raw) -> str:
    return f"{int(raw) / 10 ** 18:,.4f}" if raw is not None else "?"


def run_unified(conn, chains_list: list[config.Chain], clients: dict, reg: Registry, *, max_seconds: float = 1500.0,
                log=print, ccip_fetch=None, anomalies: list | None = None,
                ranking_every_hours: float | None = None) -> dict:
    """Do everything on `conn` (a StagedConnection) and return the summary. Raises RunFailed if the data cannot be aligned to one
    block; the caller publishes (commit) only when this returns without an exception. `anomalies` (a list the caller owns) receives
    (key, kind, detail) for everything that must be looked at by a human but does not stop the publication."""
    t0 = time.monotonic()
    chains = {c.key: c for c in chains_list}
    anoms: list = anomalies if anomalies is not None else []
    summary: dict = {"chains": {}}
    problems: list[str] = []
    codes: list[str] = []

    def block(code: str, text: str) -> None:
        codes.append(code)
        problems.append(text)

    def stop() -> None:
        if problems:
            raise RunFailed("; ".join(problems), codes)

    def left(share: float = 1.0) -> float:
        return max((max_seconds - (time.monotonic() - t0)) * share, 1.0)

    marks = {"t": t0}
    timings: dict = {}

    def tick(phase: str) -> None:
        """How long each phase took: the first thing to look at if a run is slower than expected."""
        now_ = time.monotonic()
        timings[phase] = round(now_ - marks["t"], 1)
        marks["t"] = now_
        log(f"[timing] {phase}: {timings[phase]}s")

    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(%s), now()", (ADVISORY_KEY,))
        got, tx_now = cur.fetchone()
    conn.commit()
    if not got:
        summary["skipped"] = "another unified run is in progress"
        return summary
    summary["started"] = _ts(tx_now)
    prev_ok = last_ok_start(conn)
    log(f"unified run started; previous published run: {prev_ok}")

    db.ensure_chains(conn, chains_list)
    enrich_mod.sync_registry(conn, reg, config.load_chains())
    conn.commit()
    enrich_before = {k: enrich_mod.get_enrich_state(conn, c.chain_id)[0] for k, c in chains.items()}
    conn.commit()

    # 1. transfers ---------------------------------------------------------------------------------------------
    # sync_chain rescans (and replaces) the last reorg_margin blocks: remember who was in them, their balance must be read again
    pre_touched: dict[str, set[bytes]] = {}
    for key, c in chains.items():
        _, prev_last = db.get_state(conn, c.chain_id)
        conn.commit()
        if prev_last is not None:
            pre_touched[key] = touched_addresses(conn, c.chain_id, max(prev_last - c.reorg_margin, 0), prev_last)
    targets: dict[str, dict] = {}
    for key, c in chains.items():
        res = sync_chain(c, clients[key], conn, max_seconds=left(0.5), log=log)
        if res.get("skipped"):
            block(f"locked:{key}", f"{key}: another sync holds this chain (stop the separate sync workflow while the unified one runs)")
            continue
        _, last = db.get_state(conn, c.chain_id)
        ts = clients[key].get_block_timestamps([last])[last]
        lag = (res.get("head") or last) - last
        targets[key] = {"block": last, "time": datetime.fromtimestamp(ts, timezone.utc), "head": res.get("head"), "lag_blocks": lag}
        summary["chains"][key] = {"block": last, "head": res.get("head"), "lag_blocks": lag, "new_transfers": res.get("rows", 0),
                                  "time": _ts(targets[key]["time"])}
        if res.get("stopped_early") or lag > 0 and lag > c.reorg_margin:
            block(f"behind:{key}", f"{key}: not at the head of the chain ({lag:,} blocks behind) after the time budget: catch up with "
                                   f"the separate sync workflow, then start this one again")
    tick("sync")
    stop()

    # 2. supply check (anomaly only) ------------------------------------------------------------------------------
    for key, c in chains.items():
        last = targets[key]["block"]
        ours = db.supply_from_transfers(conn, c.chain_id, last)
        theirs = int(clients[key].eth_call(c.token, SUPPLY_SELECTOR, hex(last)), 16)
        log(f"[{key}] block {last:,}: supply from our transfers {ours} vs on chain {theirs} -> {'OK' if ours == theirs else 'DIFFERENT (anomaly)'}")
        if ours != theirs:
            anoms.append((f"supply:{key}:{ours - theirs}", f"supply / {key}",
                          f"block {last:,}: our transfers give {_amount(ours)} RIZE, totalSupply() says {_amount(theirs)} (difference {_amount(ours - theirs)})"))
    summary["supply_anomalies"] = sum(1 for a in anoms if a[1].startswith("supply"))
    tick("supply check")

    # 3. trades and liquidity moves ------------------------------------------------------------------------------
    for key, c in chains.items():
        enrich_mod.enrich_chain(c, clients[key], conn, reg, max_seconds=left(0.6), log=log)
        enrich_mod.repair_traders(c, clients[key], conn, reg, max_seconds=left(0.3), log=log)
        with conn.cursor() as cur:
            cur.execute("""SELECT count(*) FROM pool_snapshots WHERE chain_id = %s AND ts = date_trunc('hour', now())""",
                        (c.chain_id,))
            photographed = cur.fetchone()[0]
        conn.commit()
        if not photographed:
            enrich_mod.snapshot(c, clients[key], conn, reg, log)
        conn.commit()                                              # get_enrich_state ends with a rollback: nothing may be pending
        last_e = enrich_mod.get_enrich_state(conn, c.chain_id)[0]
        conn.commit()
        if last_e is None or last_e < targets[key]["block"]:
            block(f"enrich:{key}", f"{key}: trades / liquidity moves stopped at block {last_e} (transfers at {targets[key]['block']}): "
                                   f"time budget or storage limit")
        with conn.cursor() as cur:
            cur.execute("SELECT tx_hash, block_number, detail FROM enrich_review WHERE chain_id = %s AND reason = 'crash' "
                        "ORDER BY block_number LIMIT %s", (c.chain_id, MAX_ANOMALIES_PER_KIND))
            crashes = cur.fetchall()
        conn.commit()
        for txh, blk, detail in crashes:
            anoms.append((f"crash:{key}:0x{bytes(txh).hex()}", f"crash / {key}",
                          f"block {blk:,} tx 0x{bytes(txh).hex()} could not be read by the market layer ({(detail or '')[:120]})"))
        summary["chains"][key]["crashes"] = len(crashes)
    tick("trades and liquidity")
    stop()

    # 3b. names of the unknown tokens ("other assets"): never blocks a run ----------------------------------------------
    try:
        # only what traded since the previous published run (cheap); the `tokens` command catches up on the whole history
        summary["tokens"] = tokens_mod.discover(conn, clients, chains, reg, log,
                                                 since=None if prev_ok is None else prev_ok - timedelta(hours=2))
    except Exception as e:  # noqa: BLE001 - naming tokens is a nicety, the run goes on
        conn.rollback()
        summary["tokens"] = {"error": type(e).__name__}
        log(f"tokens: skipped ({type(e).__name__}: {str(e)[:120]})")
    conn.commit()
    tick("tokens")

    # 4. CCIP -----------------------------------------------------------------------------------------------------
    ccip_stats: dict = {}
    for _ in range(50):
        kw = {"fetch": ccip_fetch} if ccip_fetch else {}
        st = ccip_sync(conn, chains_list, reg, limit=300, max_seconds=left(0.6), log=log, grace_minutes=CCIP_GRACE_MINUTES, **kw)
        for k, v in st.items():
            ccip_stats[k] = ccip_stats.get(k, 0) + v
        if not (st["burns_checked"] + st["mints_checked"]) or left() < 30:
            break
    conn.commit()
    backlog = ccip_backlog(conn, chains_list, CCIP_GRACE_MINUTES)
    conn.commit()
    if backlog:
        block("ccip:backlog", f"CCIP: {backlog} burn / mint transaction(s) not looked up yet (time budget): run the separate ccip "
                              f"command, then start this one again")
    cutoffs = {c.chain_id: targets[k]["time"] for k, c in chains.items()}
    rep = bridge_report(conn, since=None, cutoffs=cutoffs, quiet=True)   # everything: recent first run, history once
    conn.commit()
    for f in rep["flagged"][:MAX_ANOMALIES_PER_KIND]:
        anoms.append((f"ccip:{f['reason']}:{f['message']}", f"ccip / {f['reason']}",
                      f"message {f['message'][:14]}.. {f['source']}->{f['dest']} sent {f['sent']}  amount {_amount(f['amount'])}  "
                      f"burned {_amount(f['burned'])}  minted {_amount(f['minted'])}  send {f['send_tx']}  receive {f['receive_tx']}"))
    summary["ccip"] = {**ccip_stats, "messages": rep["messages"], "matched": rep["matched"], "in_flight": rep["pending_or_failed"],
                       "ahead_of_sync": rep["ahead_of_sync"], "anomalies": len(rep["flagged"]), "backlog": backlog}
    tick("ccip")
    stop()

    # 5. balances: every run, cheap (only the addresses that moved); stored, not displayed ------------------------------
    balances = update_balances(conn, clients, chains, targets, log, extra=pre_touched)
    summary["balances"] = {k: {"block": v["block"], "holders": v["holders"], "read": v["read"], "seeded": v["seeded"]}
                           for k, v in balances.items()}
    for key, v in balances.items():
        if not v["ok"]:
            anoms.append((f"balances:{key}:{v['sum'] - v['supply']}", f"balances / {key}",
                          f"block {v['block']:,}: the balances read add up to {_amount(v['sum'])} RIZE, totalSupply() says "
                          f"{_amount(v['supply'])} (difference {_amount(v['sum'] - v['supply'])})"))
    tick("balances")

    # 6. Top Holders table (governance read + ranking): only when the published one is older than the window -------------
    every = ranking_every_hours if ranking_every_hours is not None else float(os.environ.get("RANKING_EVERY_HOURS") or RANKING_EVERY_HOURS)
    due, why = ranking_due(conn, chains, tx_now, every)
    if due:
        log(f"Top Holders table: rebuilt in this run ({why})")
        summary["holders"] = update_ranking(conn, clients, chains, balances, log)
        summary["holders"]["rebuilt"] = True
        if summary["holders"]["governance_diff"] != 0:
            d = summary["holders"]["governance_diff"]
            anoms.append((f"governance:{d}", "governance", f"Base block {summary['holders']['base_block']:,}: bonds vs contract balance "
                                                           f"differ by {_amount(d)} RIZE"))
    else:
        log(f"Top Holders table: kept as published ({why})")
        summary["holders"] = {"rebuilt": False, "reason": why}
    tick("top holders")

    # 7. alignment checks, record, done -------------------------------------------------------------------------------
    for text in structural_checks(conn, chains, targets, balances, tx_now, ranking_done=due):
        block("alignment", text)
    stop()
    summary["anomalies"] = len(anoms)
    tick("checks")
    summary["timings"] = timings
    summary["seconds"] = round(time.monotonic() - t0, 1)
    record(conn, tx_now, True, summary, None)
    return summary
