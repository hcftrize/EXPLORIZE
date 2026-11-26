"""Liquidity managers in general (not only the Proxy Beacon): every shape of "swap and liquidity move in one transaction",
the correction of the whole history (marker, resumable selection, price outliers) and the V4 / Infinity case."""
import datetime as dt
import json

from indexer import enrich, reprocess
from indexer.config import load_chains
from indexer.logs import TRANSFER_TOPIC0
from indexer.receipts import BURN_TOPICS, COLLECT_TOPIC, MINT_TOPICS, SWAP_TOPICS, reconcile
from indexer.registry import Registry
from tests import test_beacon_rebalance as bb
from tests.test_enrich import FakeNode, _seed_transfers
from tests.test_venues import (ETH_RIZE, ID_ETH_RIZE, MANAGER, UNISWAP_V4_SWAP, WALLET, Rec, run, t32, w)

RIZE, USDC, WETH = bb.RIZE, bb.USDC, bb.WETH
V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V2_MINT = "0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f"
V2_BURN = "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496"
PAIR = "0x" + "7a" * 20                  # a pool nobody registered
MGR = "0x" + "c1" * 20                   # a liquidity manager
BOT = "0x" + "b0" * 20                   # a JIT liquidity bot
VICTIM = "0x" + "d1" * 20
E18, U6 = 10 ** 18, 10 ** 6


class Rcpt:
    def __init__(self):
        self.logs = []

    def _add(self, addr, topics, data=""):
        self.logs.append({"address": addr, "topics": topics, "data": "0x" + data, "logIndex": hex(len(self.logs))})
        return self

    def transfer(self, token, a, b, n):
        return self._add(token, [TRANSFER_TOPIC0, t32(a), t32(b)], w(n))

    def v2_swap(self, pool, a0in, a1in, a0out, a1out):
        return self._add(pool, [V2_SWAP, t32(MGR), t32(MGR)], "".join(w(x) for x in (a0in, a1in, a0out, a1out)))

    def v2_mint(self, pool, a0, a1):
        return self._add(pool, [V2_MINT, t32(MGR)], w(a0) + w(a1))

    def v2_burn(self, pool, a0, a1):
        return self._add(pool, [V2_BURN, t32(MGR), t32(MGR)], w(a0) + w(a1))

    def v3_swap(self, pool, a0, a1):
        return self._add(pool, [sorted(SWAP_TOPICS)[0], t32(MGR), t32(MGR)], "".join(w(x) for x in (a0, a1, 2 ** 90, 10 ** 20, 1)))

    def v3_mint(self, pool, a0, a1):
        return self._add(pool, [sorted(MINT_TOPICS)[1] if False else bb.MINT_V3, t32(MGR), "0x" + "00" * 32, "0x" + "00" * 32],
                         "".join(w(x) for x in (int(MGR, 16), 10 ** 15, a0, a1)))

    def v3_burn(self, pool, a0, a1):
        return self._add(pool, [bb.BURN_V3, t32(MGR), "0x" + "00" * 32, "0x" + "00" * 32], "".join(w(x) for x in (10 ** 15, a0, a1)))

    def v3_collect(self, pool, a0, a1):
        return self._add(pool, [COLLECT_TOPIC, t32(MGR), "0x" + "00" * 32, "0x" + "00" * 32], "".join(w(x) for x in (int(MGR, 16), a0, a1)))

    def done(self):
        return {"logs": self.logs, "status": "0x1", "blockNumber": "0x10"}


# ------------------------------------------------------------------------------------------ other shapes of the same transaction
def test_a_v2_pair_that_burns_swaps_and_mints_in_one_transaction():
    """Uniswap-V2 style pool (token0 USDC, token1 RIZE): the Burn pays out BEFORE its event, the Mint is paid before its event."""
    r = (Rcpt().transfer(USDC, PAIR, MGR, 2 * U6).transfer(RIZE, PAIR, MGR, 1000 * E18).v2_burn(PAIR, 2 * U6, 1000 * E18)
         .transfer(USDC, MGR, PAIR, 100_000).transfer(RIZE, PAIR, MGR, 50 * E18).v2_swap(PAIR, 100_000, 0, 0, 50 * E18)
         .transfer(USDC, MGR, PAIR, 2_100_000).transfer(RIZE, MGR, PAIR, 1050 * E18).v2_mint(PAIR, 2_100_000, 1050 * E18).done())
    x = reconcile(r, RIZE, WETH, tx_from=MGR)
    assert x.verdict == "buy" and x.traders == [MGR] and x.pool_rize == -50 * E18
    assert x.nets[PAIR][RIZE] == -50 * E18 and x.nets[PAIR][USDC] == 100_000
    assert x.flags == []


def test_a_jit_liquidity_bot_around_somebody_elses_swap_leaves_the_swap_alone():
    """The bot mints just before a victim's sale and burns + collects right after, all in one transaction."""
    r = (Rcpt().transfer(USDC, BOT, PAIR, 50 * U6).transfer(RIZE, BOT, PAIR, 20_000 * E18).v3_mint(PAIR, 50 * U6, 20_000 * E18)
         .transfer(RIZE, VICTIM, PAIR, 1000 * E18).transfer(USDC, PAIR, VICTIM, 2_300_000).v3_swap(PAIR, -2_300_000, 1000 * E18)
         .v3_burn(PAIR, 47_700_000, 21_000 * E18)
         .transfer(USDC, PAIR, BOT, 47_700_000).transfer(RIZE, PAIR, BOT, 21_000 * E18).v3_collect(PAIR, 47_700_000, 21_000 * E18).done())
    x = reconcile(r, RIZE, WETH, tx_from=BOT, known_pools={PAIR})
    assert x.verdict == "sell" and x.traders == [VICTIM] and x.pool_rize == 1000 * E18
    assert x.nets[PAIR] == {RIZE: 1000 * E18, USDC: -2_300_000}
    assert BOT not in x.nets                                            # its flows nets to nothing once set aside


def test_a_manager_that_swaps_on_an_unregistered_pool_while_it_rebalances_the_registered_one():
    """The liquidity events are not read for amounts (the pool is unknown) but the trade is still the swap alone."""
    rz, us = 300 * E18, 700_000
    r = (Rcpt().transfer(USDC, bb.AERO1, MGR, 5 * U6).transfer(RIZE, bb.AERO1, MGR, 900 * E18).v3_collect(bb.AERO1, 5 * U6, 900 * E18)
         .transfer(USDC, MGR, PAIR, us).transfer(RIZE, PAIR, MGR, rz).v2_swap(PAIR, us, 0, 0, rz)
         .transfer(USDC, MGR, bb.AERO1, 6 * U6).transfer(RIZE, MGR, bb.AERO1, 1200 * E18).v3_mint(bb.AERO1, 6 * U6, 1200 * E18).done())
    x = reconcile(r, RIZE, WETH, tx_from=MGR, known_pools={bb.AERO1, PAIR})
    assert x.verdict == "buy" and x.venues == [PAIR] and x.traders == [MGR] and x.pool_rize == -rz
    assert bb.AERO1 not in x.nets or not x.nets[bb.AERO1].get(RIZE)


def test_a_plain_swap_next_to_a_plain_swap_is_untouched():
    r = (Rcpt().transfer(RIZE, VICTIM, PAIR, 500 * E18).transfer(USDC, PAIR, VICTIM, 1_000_000).v2_swap(PAIR, 0, 500 * E18, 1_000_000, 0).done())
    x = reconcile(r, RIZE, WETH, tx_from=VICTIM)
    assert x.verdict == "sell" and x.lp_set_aside == [] and x.flags == []


# ------------------------------------------------------------------------------------------ V4 / Infinity: the side comes from the swap events
def test_a_v4_holder_that_swaps_and_adds_liquidity_keeps_the_side_of_its_swap():
    """The manager's net RIZE is +4,000 (5,000 added, 1,000 bought): read from the nets this is a sale. The swap is a BUY of 1,000."""
    rz = 1000 * E18
    rec = (Rec().transfer(ETH_RIZE, MANAGER, WALLET, rz)
           .v4_swap(MANAGER, UNISWAP_V4_SWAP, ID_ETH_RIZE, -3_835_182_910_893_151, rz)
           .transfer(ETH_RIZE, WALLET, MANAGER, 5000 * E18)
           .modify(MANAGER, ID_ETH_RIZE, 98000, 138000, 10 ** 19, 7).done())
    r, _ = run(rec, "ethereum", frm=WALLET)
    assert r.verdict == "buy" and r.pool_rize == -rz and r.traders == [WALLET]
    assert r.fills[0]["rize"] == rz and r.fills[0]["dir"] == -1
    assert any(f.startswith("events_vs_transfers") for f in r.flags)    # the net still disagrees with the swap: kept for review


# ------------------------------------------------------------------------------------------ the whole history, on a database
TS = dt.datetime(2026, 11, 20, tzinfo=dt.timezone.utc)
BLOCK = 62_558_789


def _beacon_setup(conn, key="74d75d40"):
    """What an older reading stored for a Beacon rebalance: a phantom BUY of 1,615 RIZE at $0.0148 and a doubled removal, no marker."""
    chain = load_chains()["base"]
    tx = "0x" + key + "ab" * 28
    rec = bb.receipt(bb.TXS[key])
    rec["blockNumber"] = hex(BLOCK)
    _seed_transfers(conn, 8453, tx, BLOCK, [(RIZE, bb.BEACON, bb.AERO1, 1)])
    with conn.cursor() as cur:
        cur.execute("INSERT INTO chain_state (chain_id, slug, token, start_block, last_block) VALUES (8453,'base',%s,1,%s)",
                    (bytes.fromhex(RIZE[2:]), BLOCK + 10))
        txb = bytes.fromhex(tx[2:])
        cur.execute("""INSERT INTO trades (chain_id, tx_hash, block_number, block_time, side, trader, router, rize, usd, price_usd, extra)
                       VALUES (8453,%s,%s,%s,'buy',%s,%s,1615397000000000000000,23.92,0.0148,'{}')""",
                    (txb, BLOCK, TS, bytes.fromhex(bb.BEACON[2:]), bytes.fromhex(bb.BEACON[2:])))
        for seq, kind, rz in ((1, "remove", -41487692123641225658998), (2, "collect", -41487692123641225658998), (3, "add", 45396055838140254449585)):
            cur.execute("""INSERT INTO liquidity_events (chain_id, tx_hash, seq, block_number, block_time, pool_key, kind, provider, rize, counter, detail)
                           VALUES (8453,%s,%s,%s,%s,%s,%s,%s,%s,'{}','{"tx_verdict": "buy"}')""",
                        (txb, seq, BLOCK, TS, bytes.fromhex(bb.AERO1[2:]), kind, bytes.fromhex(bb.BEACON[2:]), rz))
    conn.commit()
    return chain, tx, FakeNode({tx: rec})


def _rows(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT side, rize, price_usd, extra FROM trades")
        trades = cur.fetchall()
        cur.execute("SELECT kind, rize, detail FROM liquidity_events ORDER BY seq")
        liq = cur.fetchall()
    conn.rollback()
    return trades, liq


def test_the_lp_txs_selector_finds_the_old_rows_rewrites_them_and_then_stops_finding_them(conn):
    chain, tx, node = _beacon_setup(conn)
    reg = Registry.from_dir()
    sel = reprocess.build_selection("none", None, None, None, None, None, lp_txs=True)
    assert not sel.empty() and sel.reasons == ()
    rows, per = reprocess.select_txs(conn, chain, reg, sel)
    assert [h for h, *_ in rows] == [tx] and per == {"lp_txs": 1}

    res = reprocess.reprocess_chain(chain, node, conn, reg, sel, log=lambda m: None)
    assert res["selected"] == 1 and res["rewritten"] == 1 and res["changed"] == {"trades_changed": 1, "liquidity_changed": 1}
    trades, liq = _rows(conn)
    assert [(t[0], t[1]) for t in trades] == [("buy", 5523761014951861897839)]       # the swap, not the 1,615 RIZE of the mix
    assert round(float(trades[0][2]), 5) == 0.00233 and trades[0][3]["rv"] == enrich.READING
    assert [(k, r) for k, r, _ in liq] == [("remove", -41487692123641225658998), ("add", 45396055838140254449585)]   # no second removal
    assert all(d["rv"] == enrich.READING for *_, d in liq)

    again = reprocess.reprocess_chain(chain, node, conn, reg, sel, log=lambda m: None)   # rewritten rows carry the marker: nothing left
    assert again["selected"] == 0


def test_an_interrupted_run_continues_where_it_stopped(conn):
    chain, tx, node = _beacon_setup(conn, "74d75d40")
    second = "0x" + "58578beb" + "cd" * 28
    rec2 = bb.receipt(bb.TXS["58578beb"])
    rec2["blockNumber"] = hex(BLOCK + 1)
    _seed_transfers(conn, 8453, second, BLOCK + 1, [(RIZE, bb.BEACON, bb.AERO1, 1)])
    with conn.cursor() as cur:
        cur.execute("""INSERT INTO liquidity_events (chain_id, tx_hash, seq, block_number, block_time, pool_key, kind, provider, rize, counter, detail)
                       VALUES (8453,%s,1,%s,%s,%s,'remove',%s,-1,'{}','{}')""",
                    (bytes.fromhex(second[2:]), BLOCK + 1, TS, bytes.fromhex(bb.AERO1[2:]), bytes.fromhex(bb.BEACON[2:])))
    conn.commit()
    node.receipts[second] = rec2
    reg = Registry.from_dir()
    sel = reprocess.build_selection("none", None, None, None, None, None, lp_txs=True)
    # the first run only has time for one batch of zero transactions (time budget 0 s): nothing is rewritten, nothing is lost
    part = reprocess.reprocess_chain(chain, node, conn, reg, sel, max_seconds=-1, log=lambda m: None)
    assert part["rewritten"] == 0
    full = reprocess.reprocess_chain(chain, node, conn, reg, sel, log=lambda m: None)
    assert full["selected"] == 2 and full["rewritten"] == 2
    assert reprocess.reprocess_chain(chain, node, conn, reg, sel, log=lambda m: None)["selected"] == 0


def test_the_outliers_selector_finds_trades_far_from_the_price_of_their_day(conn):
    chain = load_chains()["base"]
    reg = Registry.from_dir()
    with conn.cursor() as cur:
        rows = [(bytes([i]) * 32, 0.0023) for i in range(1, 8)] + [(b"\xee" * 32, 0.0148), (b"\xef" * 32, 0.0023)]
        for h, price in rows:
            cur.execute("INSERT INTO trades (chain_id, tx_hash, block_number, block_time, side, rize, price_usd, extra)"
                        " VALUES (8453,%s,5,%s,'buy',1,%s,'{}')", (h, TS, price))
        cur.execute("INSERT INTO trades (chain_id, tx_hash, block_number, block_time, side, rize, price_usd, extra)"
                    " VALUES (8453,%s,5,%s,'buy',1,0.0148,%s)", (b"\xaa" * 32, TS, json.dumps({"rv": enrich.READING})))   # already read by the new code
    conn.commit()
    rows, per = reprocess.select_txs(conn, chain, reg, reprocess.build_selection("none", None, None, None, None, None, outliers=True))
    assert [h for h, *_ in rows] == ["0x" + "ee" * 32] and per == {"outliers": 1}


def test_with_no_selector_at_all_the_default_is_still_the_review_queue_and_the_new_ones_are_selectors():
    assert reprocess.build_selection(None, None, None, None, None, None).reasons == ("unknown_pool", "guess", "flag")
    assert reprocess.build_selection(None, None, None, None, None, None, lp_txs=True).reasons == ()
    assert reprocess.build_selection(None, None, None, None, None, None, outliers=True).reasons == ()


def test_every_row_written_carries_the_reading_marker(conn):
    chain, tx, node = _beacon_setup(conn)
    reg = Registry.from_dir()
    enrich.process_txs(chain, node, conn, reg, [(tx, BLOCK, TS)])
    trades, liq = _rows(conn)
    assert trades[0][3]["rv"] == enrich.READING and all(d["rv"] == enrich.READING for *_, d in liq)
