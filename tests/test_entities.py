"""Entities (everything an entity holds, added up) and the automatic naming of unknown tokens."""
import psycopg2
import psycopg2.extras

from indexer import config, entities, holders, tokens
from indexer.multicall import SEL_AGGREGATE3
from tests.test_governance import encode_results, parse_aggregate3, word
from tests.test_holders import E18, add_transfers, make_clients, q

K1, K2, K3 = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
POOL, MANAGER, TEAM = "0x" + "44" * 20, "0x" + "55" * 20, "0x" + "66" * 20


def b(a):
    return psycopg2.Binary(bytes.fromhex(a[2:]))


def seed_labels(conn):
    chains = config.load_chains()
    base, eth = chains["base"].chain_id, chains["ethereum"].chain_id
    with conn.cursor() as cur:
        cur.execute("DELETE FROM labels; DELETE FROM pools; DELETE FROM venues")
        rows = [(K1, base, "Kraken (0x111)", "cex", "Kraken"), (K2, 0, "Kraken (0x222)", "cex", "Kraken"),
                (K3, eth, "MEXC Deposit (0x333)", "cex", "MEXC"), (TEAM, base, "Team 1", "team", "Team")]
        for a, cid, lab, cat, ent in rows:
            cur.execute("INSERT INTO labels (address, chain_id, label, category, entity) VALUES (%s,%s,%s,%s,%s)",
                        (b(a), cid, lab, cat, ent))
        cur.execute("INSERT INTO venues VALUES ('aero', 'Aero', 'dex', 'aero')")
        cur.execute("INSERT INTO pools (chain_id, pool_key, name, venue, kind, holder) VALUES (%s,%s,'Aero 1','aero','v3',NULL)",
                    (base, b(POOL)))
        cur.execute("INSERT INTO pools (chain_id, pool_key, name, venue, kind, manager) VALUES (%s,%s,'Aero 2','aero','v4',%s)",
                    (base, psycopg2.Binary(bytes([7]) * 32), b(MANAGER)))
    conn.commit()
    return chains


def test_groups_add_up_every_address_and_every_pool_of_a_venue(conn):
    chains = seed_labels(conn)
    g = entities.load_groups(conn, chains)
    assert set(g) == {"Kraken", "MEXC", "Aero"}                      # the team is left out
    assert g["Kraken"]["members"] == {("base", K1): "Kraken (0x111)", (None, K2): "Kraken (0x222)"}
    assert set(g["Aero"]["members"]) == {("base", POOL), ("base", MANAGER)}

    held = {K1: {"base": 100 * E18}, K2: {"base": 5 * E18, "bsc": 7 * E18}, K3: {"ethereum": 40 * E18},
            POOL: {"base": 300 * E18}, MANAGER: {"base": 20 * E18}, TEAM: {"base": 9_999 * E18}}
    rows, members = entities.compute(g, held, {K1: {"bonded": 1 * E18, "queued": 2 * E18, "vested_locked": 0, "count": 1}},
                                     ["base", "ethereum", "bsc", "polygon"])
    assert [(r["rank"], r["entity"], r["addresses"], r["total"] // E18) for r in rows] == [
        (1, "Aero", 2, 320), (2, "Kraken", 2, 115), (3, "MEXC", 1, 40)]
    kraken = rows[1]
    assert kraken["held"] == 112 * E18 and kraken["bonded"] == 3 * E18
    assert kraken["held_by_chain"] == {"base": str(105 * E18), "bsc": str(7 * E18)}     # K2 counts on every chain
    assert {(m["entity"], m["chain"], m["address"]) for m in members if m["entity"] == "Kraken"} == {
        ("Kraken", "base", K1), ("Kraken", "base", K2), ("Kraken", "bsc", K2)}


def test_an_entity_that_holds_nothing_is_hidden_and_the_zero_address_never_counts(conn):
    chains = seed_labels(conn)
    g = entities.load_groups(conn, chains)
    rows, _ = entities.compute(g, {K1: {"base": 0}, entities.ZERO: {"base": 5 * E18}}, {}, ["base"])
    assert rows == []


def test_the_entities_are_written_with_the_top_holders_snapshot(conn):
    seed_labels(conn)
    add_transfers(conn)
    clients = make_clients()
    base = clients["base"]._fake
    base.balances[int(K1[2:], 16)] = 60 * E18
    with conn.cursor() as cur:
        cur.execute("INSERT INTO transfers VALUES (%s, 99, now(), %s, 0, %s, %s, %s)",
                    (config.load_chains()["base"].chain_id, (99).to_bytes(32, "big"), bytes(20), bytes.fromhex(K1[2:]), E18))
    conn.commit()
    assert holders.run(clients, config.load_chains(), conn, lambda m: None)
    rows = q(conn, "SELECT rank, entity, addresses, total FROM entity_holdings ORDER BY rank")
    assert [(r[0], r[1], r[2], int(r[3]) // E18) for r in rows] == [(1, "Kraken", 1, 60)]
    assert q(conn, "SELECT entity, label FROM entity_members") == [("Kraken", "Kraken (0x111)")]
    holders.run(clients, config.load_chains(), conn, lambda m: None)                       # replaced, never duplicated
    assert q(conn, "SELECT count(*) FROM entity_holdings")[0][0] == 1


# ------------------------------------------------------------------------------------------------ tokens
def abi_string(s: str) -> bytes:
    raw = s.encode()
    return word(32) + word(len(raw)) + raw + b"\x00" * (-len(raw) % 32)


class TokenNode:
    """symbol() / name() / decimals() of a few contracts, through Multicall3."""
    TOKENS = {"0x" + "a1" * 20: ("DAI", "Dai Stablecoin", 18), "0x" + "a2" * 20: ("WETH", "Wrapped Ether", 18),
              "0x" + "a3" * 20: ("RIZE", "Fake rize", 18)}

    def eth_call(self, to, data, block="latest"):
        assert data[2:10] == SEL_AGGREGATE3
        out = []
        for target, cd in parse_aggregate3(data):
            t = self.TOKENS.get(target)
            if t is None:
                out.append((False, b""))
            elif cd.hex() == tokens.SEL_SYMBOL:
                out.append((True, abi_string(t[0])))
            elif cd.hex() == tokens.SEL_NAME:
                out.append((True, abi_string(t[1])))
            else:
                out.append((True, word(t[2])))
        return encode_results(out)


def test_unknown_tokens_get_their_symbol_from_the_contract(conn):
    chains = config.load_chains()
    base = chains["base"]
    dai, weth, fake, dead = "0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20, "0x" + "a4" * 20
    with conn.cursor() as cur:
        cur.execute("DELETE FROM trades; DELETE FROM tokens; DELETE FROM pools")
        cur.execute("INSERT INTO tokens (chain_id, address, symbol, decimals, source) VALUES (%s,%s,'WETH',18,'file')",
                    (base.chain_id, b(weth)))                                   # already named by the owner: not asked again
        for n, ctr in enumerate([{dai[2:]: "5", weth[2:]: "1"}, {fake[2:]: "9", dead[2:]: "1", "0" * 40: "3"}], 1):
            cur.execute("INSERT INTO trades (chain_id, tx_hash, block_number, block_time, side, rize, counter) "
                        "VALUES (%s,%s,%s,now(),'buy',1,%s)", (base.chain_id, n.to_bytes(32, "big"), n, psycopg2.extras.Json(ctr)))
    conn.commit()
    assert tokens.unknown_tokens(conn, [base.chain_id]) == {base.chain_id: sorted([dai, fake, dead])}
    reg = type("R", (), {"rize": staticmethod(lambda k: "0x" + "ff" * 20)})()
    logs = []
    res = tokens.discover(conn, {"base": TokenNode()}, {"base": base}, reg, logs.append)
    assert res == {"found": 2, "unreadable": 1, "left": 0}
    rows = {bytes(r[0]).hex(): r[1:] for r in q(conn, "SELECT address, symbol, name, decimals, source FROM tokens WHERE chain_id = %s",
                                                (base.chain_id,))}
    assert rows["a1" * 20] == ("DAI", "Dai Stablecoin", 18, "auto")
    assert rows["a3" * 20][0] == "RIZE*"                                         # an impostor never shows as plain RIZE
    assert rows["a4" * 20] == ("", None, None, "unreadable")
    assert rows["a2" * 20][3] == "file"
    assert tokens.unknown_tokens(conn, [base.chain_id]) == {}                    # nothing is asked twice
    with conn.cursor() as cur:                                                   # the cheap scan of the unified run only sees recent trades
        cur.execute("INSERT INTO trades (chain_id, tx_hash, block_number, block_time, side, rize, counter) "
                    "VALUES (%s,%s,9,now() - interval '30 days','buy',1,%s)",
                    (base.chain_id, (9).to_bytes(32, "big"), psycopg2.extras.Json({"b1" * 20: "1"})))
    conn.commit()
    import datetime as dt
    assert tokens.unknown_tokens(conn, [base.chain_id], since=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)) == {}
    assert tokens.unknown_tokens(conn, [base.chain_id]) == {base.chain_id: ["0x" + "b1" * 20]}      # the full catch-up finds it
    assert tokens.unknown_tokens(conn, [base.chain_id], retry_unreadable=True) == {base.chain_id: [dead, "0x" + "b1" * 20]}


def test_text_read_from_a_contract_is_cleaned():
    assert tokens.clean("DA‮I\x00\n", 24) == "DAI"
    assert len(tokens.clean("X" * 100, 24)) == 24
    assert tokens.dec_string(b"MKR".ljust(32, b"\x00")) == "MKR"
    assert tokens.dec_string(b"\x00" * 64) is None


# ------------------------------------------------------------------------------------------------ not pools
def test_an_address_declared_not_a_pool_is_never_a_pool(conn, tmp_path):
    import json, shutil
    from pathlib import Path
    from indexer import enrich
    from indexer.registry import Registry
    d = tmp_path / "labels"
    shutil.copytree(Path(__file__).resolve().parent.parent / "labels", d)
    proxy = "0x" + "9a" * 20
    pools = json.loads((d / "pools.json").read_text())
    pools["not_pools"] = [{"chain": "base", "key": proxy, "note": "Arrakis proxy"}]
    (d / "pools.json").write_text(json.dumps(pools))
    ents = json.loads((d / "entities.json").read_text())
    ents["entities"].append({"address": proxy, "chain": "base", "label": "Arrakis : Proxy (0x9a9)", "category": "liquidity-manager",
                             "entity": "Arrakis"})
    (d / "entities.json").write_text(json.dumps(ents))
    reg = Registry.from_dir(d)
    chains = config.load_chains()
    base = chains["base"].chain_id
    with conn.cursor() as cur:                                   # the code had taken it for a pool earlier
        cur.execute("DELETE FROM pools; DELETE FROM labels")
        cur.execute("INSERT INTO pools (chain_id, pool_key, name, venue, kind, source) VALUES (%s,%s,'POOL UNKNW 9','','v2','auto')",
                    (base, b(proxy)))
        cur.execute("INSERT INTO labels (address, chain_id, label, category) VALUES (%s,%s,'POOL UNKNW 9','pool')", (b(proxy), base))
    conn.commit()
    enrich.sync_registry(conn, reg, chains)
    assert q(conn, "SELECT count(*) FROM pools WHERE pool_key = %s", (b(proxy),)) == [(0,)]
    assert q(conn, "SELECT label, category, entity FROM labels WHERE address = %s", (b(proxy),)) == [
        ("Arrakis : Proxy (0x9a9)", "liquidity-manager", "Arrakis")]
    assert reg.is_not_pool("base", proxy) and not reg.is_not_pool("base", "0x" + "11" * 20)
    reg.with_names_from([dict(chain="base", key=proxy, name="POOL UNKNW 9", venue="", kind="v2", token0="", token1="", fee=None,
                              tick_spacing=None, factory="", gauge="", manager="", holder="", status="active", source="auto", note="")])
    assert reg.pool("base", proxy) is None
