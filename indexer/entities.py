"""Entities: what every known entity holds, ALL its addresses added up (Kraken, MEXC, Wintermute, Aerodrome, Uniswap...).

Who belongs to an entity
  * an address of labels/entities.json that carries an "entity" field: {"label": "Kraken (0x7DA)", "entity": "Kraken", ...}
    (the label is what the tables show for that address; the entity is the group it is added to)
  * every pool of a trading venue: the pools of one venue make one entity, named like the venue ("Aerodrome", "Aero", "Uniswap",
    "PancakeSwap"...). Pools are counted through the address that really holds their tokens (the pool itself, or the shared
    manager / vault of a Uniswap V4 or PancakeSwap Infinity pool, counted once).

Left out for now: the categories in EXCLUDED_CATEGORIES (team and tokenomics wallets, the zero address) and entities that hold nothing
(routers and aggregators pass RIZE through without keeping any).

It is computed from the balances the Top Holders job already has in memory (held per address and chain, bonds per owner), and written
in the same transaction as the Top Holders table: the Entities tab always carries the same date and block as Top Holders.
"""

from __future__ import annotations

EXCLUDED_CATEGORIES = ("team", "system")
ZERO = "0x" + "00" * 20
DUST = 10 ** 14                 # 0.0001 RIZE: an entity holding less than this is not shown (routers keep rounding leftovers)


def load_groups(conn, chains: dict) -> dict[str, dict]:
    """entity -> {"category", "logo", "members": {(chain key or None for every chain, address): label shown for that address}}."""
    by_id = {c.chain_id: k for k, c in chains.items()}
    groups: dict[str, dict] = {}

    def add(name: str, category: str, logo, chain_key, addr: str, label) -> None:
        g = groups.setdefault(name, {"category": category, "logo": logo, "members": {}})
        if not g.get("logo") and logo:
            g["logo"] = logo
        g["members"].setdefault((chain_key, addr), label)

    with conn.cursor() as cur:
        cur.execute("""SELECT encode(address,'hex'), chain_id, label, category, logo, entity FROM labels
                        WHERE entity IS NOT NULL AND entity <> '' ORDER BY chain_id, entity, label""")
        for a, cid, label, category, logo, entity in cur.fetchall():
            if category in EXCLUDED_CATEGORIES:
                continue
            if cid != 0 and cid not in by_id:
                continue
            add(entity, category, logo, None if cid == 0 else by_id[cid], "0x" + a, label)
        cur.execute("""SELECT p.chain_id,
                              encode(CASE WHEN octet_length(p.pool_key) = 20 THEN coalesce(p.holder, p.pool_key)
                                          ELSE coalesce(p.holder, p.manager) END, 'hex'),
                              v.name, v.kind, v.logo
                         FROM pools p JOIN venues v ON v.key = p.venue ORDER BY p.chain_id, v.name""")
        for cid, holder, vname, vkind, vlogo in cur.fetchall():
            if holder and cid in by_id:
                add(vname, vkind or "dex", vlogo, by_id[cid], "0x" + holder, None)
    conn.rollback()
    return groups


def compute(groups: dict[str, dict], held: dict[str, dict[str, int]], per_owner: dict[str, dict],
            chain_keys: list[str]) -> tuple[list[dict], list[dict]]:
    """(entity rows ranked by total, member rows). held[address][chain key] = raw RIZE; per_owner[address] = governance bonds (Base)."""
    rows, members = [], []
    for name, g in groups.items():
        cells: dict[tuple[str, str], dict] = {}
        for (ck, addr), label in g["members"].items():
            for chain in ([ck] if ck else chain_keys):
                v = held.get(addr, {}).get(chain, 0)
                o = per_owner.get(addr) if chain == "base" else None
                b = (o["bonded"] + o["queued"]) if o else 0
                if (v or b) and addr != ZERO:
                    cells.setdefault((chain, addr), {"held": v, "bonded": b, "label": label})
        held_total = sum(c["held"] for c in cells.values())
        bonded = sum(c["bonded"] for c in cells.values())
        if held_total + bonded < DUST:
            continue
        by_chain: dict[str, int] = {}
        for (chain, _), c in cells.items():
            by_chain[chain] = by_chain.get(chain, 0) + c["held"]
        rows.append({"entity": name, "category": g["category"], "logo": g.get("logo"),
                     "addresses": len({a for _, a in cells}), "held": held_total,
                     "held_by_chain": {k: str(v) for k, v in by_chain.items() if v},
                     "bonded": bonded, "total": held_total + bonded})
        for (chain, addr), c in sorted(cells.items()):
            members.append({"entity": name, "chain": chain, "address": addr, "label": c["label"],
                            "held": c["held"], "bonded": c["bonded"]})
    rows.sort(key=lambda r: (-r["total"], r["entity"].lower()))
    for n, r in enumerate(rows, 1):
        r["rank"] = n
    return rows, members


def store(cur, rows: list[dict], members: list[dict], chains: dict) -> None:
    """Replace the previous Entities tables (called inside the Top Holders transaction)."""
    import psycopg2.extras
    ids = {k: c.chain_id for k, c in chains.items()}
    cur.execute("DELETE FROM entity_members")
    cur.execute("DELETE FROM entity_holdings")
    if rows:
        psycopg2.extras.execute_values(cur, """INSERT INTO entity_holdings
            (rank, entity, category, logo, addresses, held, held_by_chain, bonded, total) VALUES %s""",
            [(r["rank"], r["entity"], r["category"], r["logo"], r["addresses"], r["held"],
              psycopg2.extras.Json(r["held_by_chain"]), r["bonded"], r["total"]) for r in rows])
    if members:
        psycopg2.extras.execute_values(cur, """INSERT INTO entity_members (entity, chain_id, address, label, held, bonded)
            VALUES %s""", [(m["entity"], ids[m["chain"]], bytes.fromhex(m["address"][2:]), m["label"], m["held"], m["bonded"])
                           for m in members])
