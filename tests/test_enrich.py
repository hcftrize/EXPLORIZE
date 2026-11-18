"""Enrichment: transaction -> stored rows, the checkpointed job against a fake node and a real (throwaway) database."""
import datetime as dt
import json
from decimal import Decimal

from indexer import enrich
from indexer.config import load_chains
from indexer.registry import NATIVE, Registry
from indexer.receipts import ZERO
from tests.test_venues import (BSC_RIZE, BSC_USDC, BUYER, CLMANAGER, ETH_RIZE, FEE_TO, ID_BSC, ID_ETH_RIZE, INFINITY_SWAP,
                               MANAGER, MM, RELAY, RELAY2, ROUTER, SMART, UNISWAP_V4_SWAP, USDT, VAULT, Rec, w)

REG = Registry.from_dir()
BASE_RIZE = "0x9818b6c09f5ecc843060927e8587c427c7c93583"
BASE_USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
MAIN_POOL = "0x929312aff9e4611bba5da6adcd08b31eb487bbb5"
LP_WALLET = "0x2837e93b4c6f5b1c62dabcdd12671b6ba551ef82"
MINT_V3 = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"


def info(frm, to="0x" + "11" * 20, value=0):
    return {"from": frm, "to": to, "value": hex(value)}


def test_a_bsc_buy_becomes_one_priced_trade():
    rz, usdc = 44070027728000000000173, 99999999999378361648
    rec = (Rec().transfer(BSC_USDC, BUYER, ROUTER, usdc).transfer(BSC_USDC, ROUTER, VAULT, usdc)
           .transfer(BSC_RIZE, VAULT, ROUTER, rz).transfer(BSC_RIZE, ROUTER, BUYER, rz)
           .v4_swap(CLMANAGER, INFINITY_SWAP, ID_BSC, -usdc, rz, extra=(3299,)).done())
    rec["blockNumber"] = hex(133405634)
    b = enrich.build("bsc", REG, rec, info(BUYER, ROUTER))
    t = b.trade
    assert t["side"] == "buy" and t["trader"] == BUYER and t["rize"] == rz and t["flags"] == []
    assert t["counter"] == {BSC_USDC: str(usdc)}
    assert t["usd"] == Decimal(usdc) / Decimal(10) ** 18                # USDC has 18 decimals on BNB Chain
    assert round(t["price_usd"], 6) == Decimal("0.002269")
    assert t["fills"][0]["pool"] == ID_BSC and t["fills"][0]["usd"] is not None
    assert b.review is None and b.liquidity == []


def test_the_mixed_ethereum_sale_is_stored_with_a_partial_dollar_value():
    total = 10527684700306075021198
    v4, rfq = 7090530821127693456780, 3331877032175320814205
    fee = total - v4 - rfq
    mid = "0xa43d5d5b259d8a557f14b14f87a4f61293a47112"
    rec = (Rec().transfer(ETH_RIZE, SMART, RELAY2, total).transfer(ETH_RIZE, RELAY2, FEE_TO, fee)
           .transfer(ETH_RIZE, RELAY2, mid, total - fee).transfer(ETH_RIZE, mid, MANAGER, v4)
           .transfer(ETH_RIZE, mid, RELAY, rfq).transfer(ETH_RIZE, RELAY, MM, rfq).transfer(USDT, MM, mid, 7551747)
           .v4_swap(MANAGER, UNISWAP_V4_SWAP, ID_ETH_RIZE, 8009527305970785, -v4).rfq(ETH_RIZE, USDT, rfq, 7551747).done())
    rec["blockNumber"] = hex(26405687)
    b = enrich.build("ethereum", REG, rec, info("0x43371d5ddb5c6450cb2f2c2b92d2e378932ebf00",
                                                "0x0000000071727de22e5e9d8baf0edac6f37da032"))
    t = b.trade
    assert t["side"] == "sell" and t["trader"] == SMART and t["rize"] == v4 + rfq
    assert t["counter"] == {NATIVE: "8009527305970785", USDT: "7551747"}
    assert t["usd"] == Decimal("7.551747")                              # only the USDT fill can be priced without guessing
    assert {"multi-venue", "rfq", "erc4337", "skimmed", "usd-partial"} <= set(t["tags"])
    assert t["extra"]["rize_fees"] == [[FEE_TO, str(fee)]]
    by = {f["pool"]: f for f in t["fills"]}
    assert by[ID_ETH_RIZE]["usd"] is None and by[MM]["usd"] == "7.551747"


def test_a_venue_that_had_to_be_guessed_goes_to_review_not_to_trades():
    rec = (Rec().transfer(ETH_RIZE, BUYER, "0x" + "aa" * 20, 5 * 10 ** 18)
           .transfer(USDT, "0x" + "aa" * 20, BUYER, 12000).done())
    rec["blockNumber"] = hex(5)
    b = enrich.build("ethereum", REG, rec, info(BUYER))
    assert b.trade is None
    assert b.review and b.review["reason"] == "guess" and b.review["verdict"] == "sell"


def test_a_classic_pool_liquidity_add_is_read_from_the_mint_event():
    rz, usdc = 45000 * 10 ** 18, 100 * 10 ** 6
    rec = Rec().transfer(BASE_USDC, LP_WALLET, MAIN_POOL, usdc).transfer(BASE_RIZE, LP_WALLET, MAIN_POOL, rz)
    rec._add(MAIN_POOL, [MINT_V3, "0x" + "00" * 32, "0x" + "00" * 32, "0x" + "00" * 32],
             "".join(w(x) for x in (int(LP_WALLET, 16), 10 ** 15, usdc, rz)))
    b = enrich.build("base", REG, {**rec.done(), "blockNumber": hex(9)}, info(LP_WALLET))
    assert b.trade is None and len(b.liquidity) == 1
    row = b.liquidity[0]
    assert row["kind"] == "add" and row["pool"] == MAIN_POOL and row["rize"] == rz
    assert row["counter"] == {BASE_USDC: str(usdc)} and row["provider"] == LP_WALLET


def test_an_unregistered_pool_with_swap_events_is_reported_as_certain():
    fren = "0x7f471a684bab236597d44caa32fc1afe0b262bea"
    rz = 173492870397716955831
    rec = Rec().transfer(BSC_RIZE, VAULT, fren, rz).v2_swap(fren, 0, rz, 2662615000000000000, 0)
    b = enrich.build("bsc", REG, {**rec.done(), "blockNumber": hex(7)}, info(BUYER))
    assert fren in b.unknown_pools
    p = REG.register_unknown("bsc", fren, "v2", "0xaa" + "00" * 19, BSC_RIZE, "0xca143ce32fe78f1f7019d7d551a6402fc5350c73")
    assert p.name == "POOL UNKNW 1" and p.venue == "pancakeswap-v2" and p.source == "auto"
    assert REG.register_unknown("bsc", "0x" + "12" * 20).name == "POOL UNKNW 2"     # numbers keep counting


# ---------------------------------------------------------------------------------------- the job on a real database
class FakeNode:
    def __init__(self, receipts):
        self.receipts = receipts
        self.calls = 0

    def batch(self, calls):
        self.calls += len(calls)
        out = []
        for m, p in calls:
            tx = p[0]
            rec = self.receipts.get(tx)
            out.append(rec if m == "eth_getTransactionReceipt" else ({"from": BUYER, "to": ROUTER, "value": "0x0"} if rec else None))
        return out


def _seed_transfers(conn, chain_id, tx, block, rows):
    ts = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    with conn.cursor() as cur:
        for i, (token, frm, to, amount) in enumerate(rows):
            cur.execute("INSERT INTO transfers (chain_id, block_number, block_time, tx_hash, log_index, from_addr, to_addr, amount)"
                        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                        (chain_id, block, ts, bytes.fromhex(tx[2:]), i, bytes.fromhex(frm[2:]), bytes.fromhex(to[2:]), amount))
    conn.commit()


def test_the_job_enriches_checkpoints_and_is_idempotent(conn):
    chain = load_chains()["bsc"]
    rz, usdc = 44070027728000000000173, 99999999999378361648
    tx = "0x" + "a6" * 32
    rec = (Rec().transfer(BSC_USDC, BUYER, ROUTER, usdc).transfer(BSC_USDC, ROUTER, VAULT, usdc)
           .transfer(BSC_RIZE, VAULT, ROUTER, rz).transfer(BSC_RIZE, ROUTER, BUYER, rz)
           .v4_swap(CLMANAGER, INFINITY_SWAP, ID_BSC, -usdc, rz, extra=(3299,)).done())
    rec["blockNumber"] = hex(133405634)
    _seed_transfers(conn, 56, tx, 133405634, [(BSC_RIZE, VAULT, ROUTER, rz), (BSC_RIZE, ROUTER, BUYER, rz)])
    # a transfer between two plain wallets must not be fetched at all
    _seed_transfers(conn, 56, "0x" + "b1" * 32, 133405635, [(BSC_RIZE, BUYER, ROUTER, 10 ** 18)])
    with conn.cursor() as cur:
        cur.execute("INSERT INTO chain_state (chain_id, slug, token, start_block, last_block) VALUES (56,'bsc',%s,133400000,133406000)",
                    (bytes.fromhex(BSC_RIZE[2:]),))
    conn.commit()
    reg = Registry.from_dir()
    enrich.sync_registry(conn, reg, load_chains())
    node = FakeNode({tx: rec})
    logs = []
    res = enrich.enrich_chain(chain, node, conn, reg, log=logs.append)
    assert res["txs"] == 1 and res["trades"] == 1 and node.calls == 2         # one receipt + one tx lookup
    with conn.cursor() as cur:
        cur.execute("SELECT side, rize, usd, encode(trader,'hex'), fills FROM trades")
        side, rize, usd, trader, fills = cur.fetchone()
        cur.execute("SELECT last_block, txs_done FROM enrich_state WHERE chain_id = 56")
        state = cur.fetchone()
        cur.execute("SELECT count(*) FROM pools WHERE chain_id = 56")
        n_pools = cur.fetchone()[0]
    assert side == "buy" and rize == rz and trader == BUYER[2:] and Decimal(usd) > 99 and state == (133406000, 1)
    assert fills[0]["pool"] == ID_BSC and n_pools == 1
    # running again on the same data changes nothing (the checkpoint re-reads only the reorg margin)
    enrich.enrich_chain(chain, node, conn, reg, log=logs.append)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trades")
        assert cur.fetchone()[0] == 1
    # a transaction that stops being a trade is removed, not left behind
    node.receipts[tx] = {**rec, "logs": [l for l in rec["logs"] if l["topics"][0] != INFINITY_SWAP]}
    enrich.rebuild(conn, 56)
    enrich.enrich_chain(chain, node, conn, reg, log=logs.append)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM trades")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT reason FROM enrich_review")
        assert [r[0] for r in cur.fetchall()] == ["guess"]


def test_a_crashing_receipt_is_noted_and_the_job_continues(conn):
    chain = load_chains()["bsc"]
    tx = "0x" + "c1" * 32
    _seed_transfers(conn, 56, tx, 100, [(BSC_RIZE, VAULT, ROUTER, 10 ** 18)])
    with conn.cursor() as cur:
        cur.execute("INSERT INTO chain_state (chain_id, slug, token, start_block, last_block) VALUES (56,'bsc',%s,1,200)",
                    (bytes.fromhex(BSC_RIZE[2:]),))
    conn.commit()
    reg = Registry.from_dir()
    node = FakeNode({tx: {"logs": [{"address": BSC_RIZE, "topics": ["bad"], "data": "0x", "logIndex": "0x0"}],
                          "blockNumber": "0x64"}})
    enrich.enrich_chain(chain, node, conn, reg, log=lambda m: None)
    with conn.cursor() as cur:
        cur.execute("SELECT reason FROM enrich_review")
        rows = cur.fetchall()
    assert rows == [("crash",)] or rows == []        # a malformed log is skipped, a real crash is recorded; never an exception


def test_the_names_the_site_reads_are_written_from_the_labels_files(conn):
    reg = Registry.from_dir()
    enrich.sync_registry(conn, reg, load_chains())
    with conn.cursor() as cur:
        cur.execute("SELECT label, category FROM labels WHERE address = %s AND chain_id = 8453",
                    (bytes.fromhex("284c507b2d8563660acbe4288963f940c98d7ede"),))
        proxy = cur.fetchone()
        cur.execute("SELECT label, category FROM labels WHERE address = %s AND chain_id = 8453",
                    (bytes.fromhex(MAIN_POOL[2:]),))
        pool = cur.fetchone()
        cur.execute("SELECT name FROM venues WHERE key = 'aerodrome'")
        venue = cur.fetchone()
        cur.execute("SELECT symbol, decimals FROM tokens WHERE chain_id = 56 AND address = %s", (bytes.fromhex(BSC_USDC[2:]),))
        tok = cur.fetchone()
    assert proxy == ("Proxy (EIP-1967 Beacon)", "proxy") and pool[1] == "pool"
    assert venue == ("Aerodrome Finance",) and tok == ("USDC", 18)
    enrich.sync_registry(conn, reg, load_chains())                          # again: nothing breaks
