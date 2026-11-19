"""Cross-chain bridge reconciliation: RIZE burned on one chain, minted on another, proven by Chainlink CCIP.

    python -m indexer ccip [--chain all|base|ethereum|bsc|polygon] [--limit 300] [--max-minutes 10]

Two sources that must agree:

  transfers  a burn is a transfer TO the zero address, a mint a transfer FROM it (our own, complete, ground truth)
  CCIP API   https://api.ccip.chain.link/v2 : the message that links one send transaction to one receipt transaction
             (messageId, status, source and destination chain, both transaction hashes, sender, receiver, amount)

For every burn transaction not looked up yet, the API is asked by `sourceTransactionHash`; the message is stored in
`ccip_messages`. A message still on its way (SENT, VERIFYING...) is asked again by id on the next run until it ends
(SUCCESS or FAILED). `bridge_report` then says, per message, whether the amount burned on the source equals the amount
minted on the destination, and lists the burns with no CCIP message and the mints no message accounts for.

The API is public and free (be gentle: one request at a time, a short pause between them).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import psycopg2.extras

from . import config
from .registry import Registry

API = "https://api.ccip.chain.link/v2"
FINAL = {"SUCCESS", "FAILED"}
PAUSE = 0.25


def http_get(url: str, *, timeout: float = 20.0, retries: int = 4):
    """GET + JSON. 404 -> None (nothing found). 429 / 5xx -> wait and retry."""
    delay = 1.0
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "explorize-indexer"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return None


def _ts(v):
    if not v:
        return None
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def _hex(v: str | None) -> bytes | None:
    return bytes.fromhex(v[2:]) if v and v.startswith("0x") else None


def _chain_id(info) -> int:
    """EVM chains give a number ("8453"); other networks give a name ("MainNet"): those are stored as 0 (name kept in `raw`)."""
    v = (info or {}).get("chainId")
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def parse_message(m: dict, rize_by_chain: dict[int, str]) -> dict:
    """One API message -> the row stored. The amount is the one of the RIZE token (a message may carry several)."""
    src = _chain_id(m.get("sourceNetworkInfo"))
    dst = _chain_id(m.get("destNetworkInfo"))
    entries = []
    if isinstance(m.get("tokenAmounts"), list):
        entries = [(t.get("sourceTokenAddress") or t.get("tokenAddress"), t.get("amount") or t.get("tokenAmount"))
                   for t in m["tokenAmounts"]]
    sta = m.get("sourceTokenAmount")
    if isinstance(sta, dict):
        entries.append((sta.get("tokenAddress"), sta.get("tokenAmount")))
    rize = (rize_by_chain.get(src) or "").lower()
    amount = next((int(a) for t, a in entries if t and t.lower() == rize and a is not None), None)
    keep = {k: m.get(k) for k in ("messageId", "status", "sendTransactionHash", "receiptTransactionHash", "sender", "receiver",
                                  "origin", "sendTimestamp", "receiptTimestamp", "version", "sourceNetworkInfo",
                                  "destNetworkInfo")}
    return {"message_id": _hex(m["messageId"]), "status": m.get("status"), "source_chain_id": src, "dest_chain_id": dst,
            "source_tx": _hex(m.get("sendTransactionHash")), "dest_tx": _hex(m.get("receiptTransactionHash")),
            "sender": _hex(m.get("sender")), "receiver": _hex(m.get("receiver")), "amount": amount,
            "sent_at": _ts(m.get("sendTimestamp")), "received_at": _ts(m.get("receiptTimestamp")), "raw": keep}


def save_message(conn, row: dict) -> None:
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO ccip_messages (message_id, status, source_chain_id, dest_chain_id, source_tx, dest_tx, sender,
                         receiver, amount, sent_at, received_at, raw)
                       VALUES (%(message_id)s,%(status)s,%(source_chain_id)s,%(dest_chain_id)s,%(source_tx)s,%(dest_tx)s,
                               %(sender)s,%(receiver)s,%(amount)s,%(sent_at)s,%(received_at)s,%(raw)s)
                       ON CONFLICT (message_id) DO UPDATE SET status=EXCLUDED.status, dest_tx=EXCLUDED.dest_tx,
                         received_at=EXCLUDED.received_at, amount=COALESCE(EXCLUDED.amount, ccip_messages.amount),
                         raw=EXCLUDED.raw""",
                    {**row, "message_id": psycopg2.Binary(row["message_id"]),
                     "source_tx": psycopg2.Binary(row["source_tx"]) if row["source_tx"] else None,
                     "dest_tx": psycopg2.Binary(row["dest_tx"]) if row["dest_tx"] else None,
                     "sender": psycopg2.Binary(row["sender"]) if row["sender"] else None,
                     "receiver": psycopg2.Binary(row["receiver"]) if row["receiver"] else None,
                     "raw": psycopg2.extras.Json(row["raw"])})


def burns_to_check(conn, chain_id: int, limit: int) -> list[tuple[bytes, int]]:
    """Burn transactions (RIZE sent to the zero address), newest first, not looked up yet."""
    with conn.cursor() as cur:
        cur.execute("""SELECT t.tx_hash, max(t.block_number) FROM transfers t
                        WHERE t.chain_id = %s AND t.to_addr = %s
                          AND NOT EXISTS (SELECT 1 FROM ccip_checked c WHERE c.chain_id = t.chain_id AND c.tx_hash = t.tx_hash)
                        GROUP BY t.tx_hash ORDER BY max(t.block_number) DESC LIMIT %s""", (chain_id, bytes(20), limit))
        rows = [(bytes(h), b) for h, b in cur.fetchall()]
    conn.rollback()
    return rows


def mints_to_check(conn, chain_id: int, limit: int) -> list[tuple[bytes, int]]:
    """Mint transactions (RIZE created from the zero address), newest first, that no stored message accounts for and that were
    not looked up yet: these are the deliveries from networks we do not index (Canton...)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT t.tx_hash, max(t.block_number) FROM transfers t
                        WHERE t.chain_id = %s AND t.from_addr = %s
                          AND NOT EXISTS (SELECT 1 FROM ccip_messages m WHERE m.dest_chain_id = t.chain_id AND m.dest_tx = t.tx_hash)
                          AND NOT EXISTS (SELECT 1 FROM ccip_checked c WHERE c.chain_id = t.chain_id AND c.tx_hash = t.tx_hash)
                        GROUP BY t.tx_hash ORDER BY max(t.block_number) DESC LIMIT %s""", (chain_id, bytes(20), limit))
        rows = [(bytes(h), b) for h, b in cur.fetchall()]
    conn.rollback()
    return rows


def open_messages(conn, limit: int) -> list[bytes]:
    with conn.cursor() as cur:
        cur.execute("SELECT message_id FROM ccip_messages WHERE status IS NULL OR status NOT IN ('SUCCESS','FAILED') "
                    "ORDER BY sent_at NULLS LAST LIMIT %s", (limit,))
        rows = [bytes(r[0]) for r in cur.fetchall()]
    conn.rollback()
    return rows


def ccip_sync(conn, chains, reg: Registry, *, limit: int = 300, max_seconds: float | None = None, fetch=http_get,
              pause: float = PAUSE, log=print) -> dict:
    t0 = time.monotonic()
    rize_by_chain = {c.chain_id: reg.rize(c.key) for c in chains}
    stats = {"burns_checked": 0, "mints_checked": 0, "messages": 0, "no_message": 0, "refreshed": 0}

    def out_of_time() -> bool:
        return max_seconds is not None and time.monotonic() - t0 > max_seconds

    for mid in open_messages(conn, limit):                       # messages still on their way: ask again by id
        if out_of_time():
            break
        data = fetch(f"{API}/messages/0x{mid.hex()}")
        if data:
            save_message(conn, parse_message(data.get("data", data) if isinstance(data.get("data", None), dict) else data,
                                             rize_by_chain))
            conn.commit()
            stats["refreshed"] += 1
        time.sleep(pause)
    for c in chains:
        for txh, block in burns_to_check(conn, c.chain_id, limit):
            if out_of_time():
                log(f"[ccip] time budget reached; the rest is resumed at the next run")
                return stats
            q = urllib.parse.urlencode({"sourceTransactionHash": "0x" + txh.hex(), "limit": 5})
            data = fetch(f"{API}/messages?{q}")
            msgs = (data or {}).get("data") or []
            for m in msgs:
                try:
                    save_message(conn, parse_message(m, rize_by_chain))
                except Exception as e:  # noqa: BLE001 - one odd message must not stop the run
                    conn.rollback()
                    log(f"[ccip] message {m.get('messageId')} skipped: {type(e).__name__}: {e}")
            with conn.cursor() as cur:
                cur.execute("INSERT INTO ccip_checked (chain_id, tx_hash, found) VALUES (%s,%s,%s) "
                            "ON CONFLICT (chain_id, tx_hash) DO UPDATE SET found = EXCLUDED.found, checked_at = now()",
                            (c.chain_id, psycopg2.Binary(txh), bool(msgs)))
            conn.commit()
            stats["burns_checked"] += 1
            stats["messages"] += len(msgs)
            stats["no_message"] += 0 if msgs else 1
            time.sleep(pause)
    for c in chains:                                              # deliveries nobody burned for on our chains (other networks)
        for txh, block in mints_to_check(conn, c.chain_id, limit):
            if out_of_time():
                log(f"[ccip] time budget reached; the rest is resumed at the next run")
                return stats
            q = urllib.parse.urlencode({"q": "0x" + txh.hex(), "limit": 5})
            data = fetch(f"{API}/messages?{q}")
            msgs = [m for m in ((data or {}).get("data") or [])
                    if (m.get("receiptTransactionHash") or "").lower() == "0x" + txh.hex()]
            for m in msgs:
                try:
                    save_message(conn, parse_message(m, rize_by_chain))
                except Exception as e:  # noqa: BLE001
                    conn.rollback()
                    log(f"[ccip] message {m.get('messageId')} skipped: {type(e).__name__}: {e}")
            with conn.cursor() as cur:
                cur.execute("INSERT INTO ccip_checked (chain_id, tx_hash, found) VALUES (%s,%s,%s) "
                            "ON CONFLICT (chain_id, tx_hash) DO UPDATE SET found = EXCLUDED.found, checked_at = now()",
                            (c.chain_id, psycopg2.Binary(txh), bool(msgs)))
            conn.commit()
            stats["mints_checked"] += 1
            stats["messages"] += len(msgs)
            stats["no_message"] += 0 if msgs else 1
            time.sleep(pause)
    log(f"[ccip] {stats}")
    return stats


def dataset_summary(conn, log=print) -> dict:
    """What the stored messages look like (period, per month, per lane), to compare with other CCIP sources."""
    out = {}
    with conn.cursor() as cur:
        cur.execute("""SELECT count(*), count(DISTINCT source_tx), count(amount), min(sent_at), max(sent_at),
                              count(*) FILTER (WHERE status = 'SUCCESS')
                         FROM ccip_messages""")
        n, txs, with_amount, first, last, ok = cur.fetchone()
        cur.execute("""SELECT to_char(sent_at, 'YYYY-MM'), count(*), count(amount) FROM ccip_messages
                        GROUP BY 1 ORDER BY 1""")
        months = cur.fetchall()
        cur.execute("""SELECT source_chain_id, dest_chain_id, count(*), count(amount) FROM ccip_messages
                        GROUP BY 1, 2 ORDER BY 3 DESC""")
        lanes = cur.fetchall()
        cur.execute("""SELECT count(*), count(amount) FROM ccip_messages WHERE sent_at >= now() - interval '8 days'""")
        recent, recent_amount = cur.fetchone()
        cur.execute("""SELECT max(c) FROM (SELECT count(*) c FROM ccip_messages WHERE source_tx IS NOT NULL
                                            GROUP BY source_chain_id, source_tx) x""")
        biggest = cur.fetchone()[0]
    conn.rollback()
    log(f"--- stored messages: {n} ({with_amount} with a RIZE amount, {ok} SUCCESS), {txs} distinct send transactions, "
        f"at most {biggest} messages in one send transaction")
    log(f"    sent between {first} and {last}; in the last 8 days: {recent} ({recent_amount} with a RIZE amount)")
    log("    per month (messages / with RIZE amount): " + ", ".join(f"{m}: {c}/{a}" for m, c, a in months))
    log("    per lane source->dest chain id (messages / with RIZE amount): "
        + ", ".join(f"{a}->{b}: {c}/{w}" for a, b, c, w in lanes))
    out.update(messages=n, with_amount=with_amount, first=first, last=last, recent=recent)
    return out


def bridge_report(conn) -> dict:
    """Does every burn have its CCIP message and its mint? Reads only the database.

    One transaction can carry several messages (CCIP executes them in a batch): then the mint is the SUM of the
    messages that share the transaction, so each side is compared with the message amount OR with that sum.
    A network we do not index (Canton) can only be checked on the side we see, and its message may carry no amount.
    A transaction that is not in our transfers table at all cannot be checked either: it is counted apart
    ("not_in_transfers"), because that is a gap in the data, not a wrong amount."""
    dataset_summary(conn)
    with conn.cursor() as cur:
        cur.execute("""
          SELECT encode(m.message_id,'hex'), m.status, m.source_chain_id, m.dest_chain_id, encode(m.source_tx,'hex'),
                 encode(m.dest_tx,'hex'), m.amount,
                 (SELECT COALESCE(sum(amount),0) FROM transfers t WHERE t.chain_id = m.source_chain_id AND t.tx_hash = m.source_tx
                    AND t.to_addr = %(z)s) AS burned,
                 (SELECT COALESCE(sum(amount),0) FROM transfers t WHERE t.chain_id = m.dest_chain_id AND t.tx_hash = m.dest_tx
                    AND t.from_addr = %(z)s) AS minted,
                 (SELECT sum(g.amount) FROM ccip_messages g WHERE g.source_chain_id = m.source_chain_id
                    AND g.source_tx = m.source_tx) AS src_group,
                 (SELECT sum(g.amount) FROM ccip_messages g WHERE g.dest_chain_id = m.dest_chain_id
                    AND g.dest_tx = m.dest_tx) AS dst_group,
                 (SELECT count(*) FROM transfers t WHERE t.chain_id = m.source_chain_id AND t.tx_hash = m.source_tx) AS src_rows,
                 (SELECT count(*) FROM transfers t WHERE t.chain_id = m.dest_chain_id AND t.tx_hash = m.dest_tx) AS dst_rows
            FROM ccip_messages m ORDER BY m.sent_at DESC NULLS LAST""", {"z": bytes(20)})
        rows = cur.fetchall()
        cur.execute("""SELECT c.chain_id, count(*) FROM ccip_checked c WHERE NOT c.found GROUP BY c.chain_id""")
        no_msg = dict(cur.fetchall())
        cur.execute("SELECT chain_id FROM chain_state")
        indexed = {r[0] for r in cur.fetchall()}
    conn.rollback()
    matched = pending = outside = 0
    reasons: dict[str, int] = {}
    samples: dict[str, list[str]] = {}

    def flag(reason, mid, src, dst, stx, dtx, amount, burned, minted):
        reasons[reason] = reasons.get(reason, 0) + 1
        got = samples.setdefault(reason, [])
        if len(got) < 3:
            got.append(f"  [{reason}] message 0x{mid}  {src}->{dst}  ccip {amount}  burned {burned}  minted {minted}\n"
                       f"      send tx {('0x' + stx) if stx else None}\n      receive tx {('0x' + dtx) if dtx else None}")

    for mid, status, src, dst, stx, dtx, amount, burned, minted, src_group, dst_group, src_rows, dst_rows in rows:
        if status != "SUCCESS":
            pending += 1
            continue
        # each side we index: ok / "absent" (transaction not in our transfers) / "none" (no burn or mint in it) / "differs"
        def side(rows_n, moved, group, indexed_side):
            if not indexed_side:
                return None
            if not rows_n:
                return "absent"
            if moved == 0:
                return "none"
            return "ok" if (amount is None or moved == amount or moved == group) else "differs"
        s_state = side(src_rows, burned, src_group, src in indexed)
        d_state = side(dst_rows, minted, dst_group, dst in indexed)
        bad = [(n, st) for n, st in (("burn", s_state), ("mint", d_state)) if st in ("none", "differs")]
        absent = [n for n, st in (("source", s_state), ("dest", d_state)) if st == "absent"]
        if bad:
            flag(" + ".join(f"{n} {st}" for n, st in bad), mid, src, dst, stx, dtx, amount, burned, minted)
        elif absent:
            flag("not_in_transfers: " + "+".join(absent), mid, src, dst, stx, dtx, amount, burned, minted)
        elif s_state is None or d_state is None:
            if amount is None and s_state is not None:           # burn side only and the message has no amount: nothing to compare
                flag("burn side without amount", mid, src, dst, stx, dtx, amount, burned, minted)
            else:
                outside += 1
        else:
            matched += 1
    for reason, lines in samples.items():
        print(f"--- {reasons[reason]} x {reason}", flush=True)
        for line in lines:
            print(line, flush=True)
    mismatch = sum(n for r, n in reasons.items() if not r.startswith("not_in_transfers"))
    gaps = sum(n for r, n in reasons.items() if r.startswith("not_in_transfers"))
    return {"messages": len(rows), "matched": matched, "pending_or_failed": pending, "mismatch": mismatch,
            "not_in_transfers": gaps, "one_side_only_other_network": outside, "no_message_found": no_msg, "reasons": reasons}
