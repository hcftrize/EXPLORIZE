"""Tokens seen in trades, liquidity moves and pools that nobody named yet ("other assets"): read symbol / name / decimals from the contract.

The three views symbol(), name() and decimals() are read through Multicall3 on the chain's own node (dRPC): one call per 80 tokens.
What the owner wrote in labels/tokens.json always wins (it is copied to the table at every run with source 'file'); a token that was read from
its contract is stored with source 'auto'; one whose contract answers nothing sensible (not an ERC-20, self-destructed) is stored once with
source 'unreadable' so it is not asked again every run (`tokens --rebuild` asks them again).

Anything a contract says about itself is untrusted text: control characters are removed, the length is cut, and a token that is not the
registered RIZE but calls itself RIZE gets a star (impostor tokens exist; trades are matched by address, never by symbol).
"""

from __future__ import annotations

import re

import psycopg2

from .multicall import call_none, dec_uint, multicall
from .rpc import RpcError

SEL_SYMBOL = "95d89b41"
SEL_NAME = "06fdde03"
SEL_DECIMALS = "313ce567"
ZERO = "0x" + "00" * 20
PER_CALL = 80                   # tokens per Multicall3 group (3 sub-calls each)
MAX_PER_RUN = 400
_CTRL = re.compile(r"[\x00-\x1f\x7f​-‏‪-‮⁦-⁩]")


def clean(text: str, limit: int) -> str:
    return _CTRL.sub("", text).strip()[:limit]


def dec_string(b: bytes) -> str | None:
    """An ABI string, or the old bytes32 form some tokens (MKR...) still use. None when it is neither."""
    if len(b) == 32:
        raw = b.rstrip(b"\x00")
        try:
            s = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return s if s and s.isprintable() else None
    if len(b) < 64:
        return None
    off = int.from_bytes(b[:32], "big")
    if off + 32 > len(b):
        return None
    n = int.from_bytes(b[off:off + 32], "big")
    if n == 0 or n > 256 or off + 32 + n > len(b):
        return None
    try:
        return b[off + 32:off + 32 + n].decode("utf-8")
    except UnicodeDecodeError:
        return None


def unknown_tokens(conn, chain_ids: list[int], retry_unreadable: bool = False, since=None) -> dict[int, list[str]]:
    """chain id -> token addresses (0x hex) used by trades, liquidity moves or pools that the `tokens` table does not know.
    With `since` only the trades and liquidity moves of that time onward are looked at (the unified run: cheap, uses the time index);
    without it the whole history is (the `tokens` command: the catch-up of everything not named yet)."""
    with conn.cursor() as cur:
        cur.execute("""
            WITH used AS (
              SELECT chain_id, k AS a FROM trades, LATERAL jsonb_object_keys(counter) AS k WHERE (%(since)s::timestamptz IS NULL OR block_time >= %(since)s)
              UNION SELECT chain_id, k FROM liquidity_events, LATERAL jsonb_object_keys(counter) AS k WHERE (%(since)s::timestamptz IS NULL OR block_time >= %(since)s)
              UNION SELECT chain_id, encode(token0,'hex') FROM pools WHERE token0 IS NOT NULL
              UNION SELECT chain_id, encode(token1,'hex') FROM pools WHERE token1 IS NOT NULL)
            SELECT u.chain_id, lower(replace(u.a, '0x', '')) FROM used u
             WHERE u.chain_id = ANY(%(ids)s)
               AND NOT EXISTS (SELECT 1 FROM tokens t WHERE t.chain_id = u.chain_id
                                  AND encode(t.address,'hex') = lower(replace(u.a, '0x', ''))
                                  AND (t.source <> 'unreadable' OR NOT %(retry)s))
             ORDER BY 1, 2""", {"ids": chain_ids, "retry": retry_unreadable, "since": since})
        rows = cur.fetchall()
    conn.rollback()
    out: dict[int, list[str]] = {}
    for cid, a in rows:
        if len(a) == 40 and a != "0" * 40:
            out.setdefault(cid, []).append("0x" + a)
    return out


def read_tokens(client, addrs: list[str], block: str = "latest") -> dict[str, dict | None]:
    """address -> {"symbol", "name", "decimals"} or None when the contract does not answer like a token."""
    out: dict[str, dict | None] = {}
    for i in range(0, len(addrs), PER_CALL):
        part = addrs[i:i + PER_CALL]
        calls = []
        for a in part:
            calls += [(a, call_none(SEL_SYMBOL)), (a, call_none(SEL_NAME)), (a, call_none(SEL_DECIMALS))]
        res = multicall(client, calls, block, group=240)
        for n, a in enumerate(part):
            (ok_s, sym_b), (ok_n, name_b), (ok_d, dec_b) = res[3 * n:3 * n + 3]
            sym = dec_string(sym_b) if ok_s else None
            dec = dec_uint(dec_b) if ok_d and len(dec_b) >= 32 else None
            if not sym or dec is None or dec > 36 or not clean(sym, 24):
                out[a] = None
                continue
            nm = dec_string(name_b) if ok_n else None
            out[a] = {"symbol": clean(sym, 24), "name": clean(nm, 60) if nm else None, "decimals": dec}
    return out


def discover(conn, clients: dict, chains: dict, reg, log, *, retry_unreadable: bool = False, limit: int = MAX_PER_RUN, since=None) -> dict:
    """Find and store the symbols of the unknown tokens. Returns {"found": n, "unreadable": n, "left": n}."""
    ids = {c.chain_id: k for k, c in chains.items()}
    todo = unknown_tokens(conn, list(ids), retry_unreadable, since)
    rize = {c.chain_id: (reg.rize(k) or "").lower() for k, c in chains.items()}
    found = unreadable = left = 0
    budget = limit
    for cid, addrs in todo.items():
        take, left_here = addrs[:budget], max(len(addrs) - budget, 0)
        left += left_here
        budget = max(budget - len(take), 0)
        if not take:
            continue
        try:
            got = read_tokens(clients[ids[cid]], take)
        except (RpcError, ValueError) as e:
            log(f"[{ids[cid]}] tokens: could not read {len(take)} token(s) now ({type(e).__name__}: {str(e)[:100]}); next run")
            left += len(take)
            continue
        with conn.cursor() as cur:
            for a, info in got.items():
                key = psycopg2.Binary(bytes.fromhex(a[2:]))
                if info is None:
                    cur.execute("""INSERT INTO tokens (chain_id, address, symbol, decimals, source) VALUES (%s,%s,'',NULL,'unreadable')
                                   ON CONFLICT (chain_id, address) DO UPDATE SET source='unreadable'
                                   WHERE tokens.source = 'unreadable'""", (cid, key))
                    unreadable += 1
                    continue
                sym = info["symbol"]
                if sym.upper().replace("-", "") in ("RIZE", "TRIZE") and a != rize[cid]:
                    sym = sym[:23] + "*"
                cur.execute("""INSERT INTO tokens (chain_id, address, symbol, name, decimals, source) VALUES (%s,%s,%s,%s,%s,'auto')
                               ON CONFLICT (chain_id, address) DO UPDATE SET symbol=EXCLUDED.symbol, name=EXCLUDED.name,
                                 decimals=EXCLUDED.decimals, source='auto' WHERE tokens.source IN ('auto', 'unreadable')""",
                            (cid, key, sym, info["name"], info["decimals"]))
                found += 1
        conn.commit()
        log(f"[{ids[cid]}] tokens: {sum(1 for v in got.values() if v)} named from their contract, {sum(1 for v in got.values() if not v)} unreadable")
    return {"found": found, "unreadable": unreadable, "left": left}
