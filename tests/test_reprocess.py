"""Targeted reprocessing: the selectors, the dry run, the rewrite after a label change, the lock shared with the unified run."""
import datetime as dt
import os

import pytest

from indexer import enrich, reprocess
from indexer.config import load_chains
from indexer.registry import Pool, Registry
from tests.test_enrich import FakeNode, _seed_transfers, info
from tests.test_venues import (BSC_RIZE, BSC_USDC, BUYER, CLMANAGER, ETH_RIZE, ID_BSC, INFINITY_SWAP, ROUTER, USDT, VAULT, Rec)

AA = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20
TX_GUESS = "0x" + "d1" * 32
TX_OTHER = "0x" + "d2" * 32


# ------------------------------------------------------------------------------------------ the selectors as typed
def test_with_no_selector_the_default_is_the_review_queue():
    sel = reprocess.build_selection(None, None, None, None, None, None)
    assert sel.reasons == ("unknown_pool", "guess", "flag") and not sel.pools


def test_a_selector_alone_does_not_pull_in_the_review_queue():
    sel = reprocess.build_selection(None, "0x" + "ab" * 20, None, None, None, None)
    assert sel.reasons == () and sel.pools == ("0x" + "ab" * 20,)
    assert reprocess.build_selection("none", None, "0x" + "cd" * 20, None, None, None).reasons == ()
    assert reprocess.build_selection("", None, None, "price-implausible", None, None).flags == ("price-implausible",)


def test_bad_input_is_refused_with_a_clear_message():
    for kwargs in ({"reasons": "typo"}, {"pools": "0x12"}, {"pools": "hello"}, {"addresses": "0x" + "1" * 39},
                   {"txs": "0x" + "1" * 63}):
        args = dict(reasons=None, pools=None, addresses=None, flags=None, txs=None, tx_file=None)
        args.update(kwargs)
        with pytest.raises(SystemExit):
            reprocess.build_selection(**args)
    with pytest.raises(SystemExit):
        reprocess.build_selection(None, None, None, None, None, "/nonexistent/list.txt")


def test_the_transaction_list_accepts_comments_and_a_chain():
    text = f"""# special cases
    {TX_GUESS}   # wrong trader
    bsc {TX_OTHER}
    ethereum: {TX_OTHER}
    """
    assert reprocess.parse_tx_text(text) == [(None, TX_GUESS), ("bsc", TX_OTHER), ("ethereum", TX_OTHER)]
    with pytest.raises(SystemExit):
        reprocess.parse_tx_text("not a hash")


# ------------------------------------------------------------------------------------------ on a real database
def _guess_setup(conn):
    """A sale through a pool nobody has registered (stored as `guess`), plus an unrelated transaction already stored as a trade."""
    chain = load_chains()["ethereum"]
    rec = Rec().transfer(ETH_RIZE, BUYER, AA, 5 * 10 ** 18).transfer(USDT, AA, BUYER, 12000).done()
    rec["blockNumber"] = hex(5)
    _seed_transfers(conn, 1, TX_GUESS, 5, [(ETH_RIZE, BUYER, AA, 5 * 10 ** 18)])
    _seed_transfers(conn, 1, TX_OTHER, 6, [(ETH_RIZE, BUYER, OTHER, 10 ** 18)])
    with conn.cursor() as cur:
        cur.execute("INSERT INTO chain_state (chain_id, slug, token, start_block, last_block) VALUES (1,'ethereum',%s,1,100)",
                    (bytes.fromhex(ETH_RIZE[2:]),))
        cur.execute("""INSERT INTO trades (chain_id, tx_hash, block_number, block_time, side, rize) VALUES (1,%s,6,%s,'sell',1)""",
                    (bytes.fromhex(TX_OTHER[2:]), dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)))
    conn.commit()
    reg = Registry.from_dir()
    node = FakeNode({TX_GUESS: rec})
    enrich.process_txs(chain, node, conn, reg, [(TX_GUESS, 5, dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))])
    return chain, reg, node


def _stored(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT encode(tx_hash,'hex') FROM trades ORDER BY 1")
        trades = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT reason FROM enrich_review ORDER BY 1")
        reviews = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return trades, reviews


def test_a_label_that_proves_nothing_leaves_the_guess_a_guess(conn):
    chain, reg, node = _guess_setup(conn)
    reg.add_pool(Pool("ethereum", AA, "Test pool", "uniswap-v2", "v2", token0=ETH_RIZE, token1=USDT))
    sel = reprocess.build_selection(None, AA, None, None, None, None)
    res = reprocess.reprocess_chain(chain, node, conn, reg, sel, log=lambda m: None)
    assert res["selected"] == 1 and res["rewritten"] == 1 and res["changed"] == {}
    assert _stored(conn) == ([TX_OTHER[2:]], ["guess"])                # a trade needs a Swap event the code recognises


def test_a_pool_id_added_later_turns_the_guess_into_a_trade_and_nothing_else_is_touched(conn):
    from indexer.config import load_chains as lc
    chain = lc()["bsc"]
    rz, usdc = 44070027728000000000173, 99999999999378361648
    tx, other = "0x" + "e1" * 32, "0x" + "e2" * 32
    rec = (Rec().transfer(BSC_USDC, BUYER, ROUTER, usdc).transfer(BSC_USDC, ROUTER, VAULT, usdc)
           .transfer(BSC_RIZE, VAULT, ROUTER, rz).transfer(BSC_RIZE, ROUTER, BUYER, rz)
           .v4_swap(CLMANAGER, INFINITY_SWAP, ID_BSC, -usdc, rz, extra=(3299,)).done())
    rec["blockNumber"] = hex(133405634)
    _seed_transfers(conn, 56, tx, 133405634, [(BSC_RIZE, VAULT, ROUTER, rz), (BSC_RIZE, ROUTER, BUYER, rz)])
    _seed_transfers(conn, 56, other, 133405700, [(BSC_RIZE, VAULT, ROUTER, rz)])
    reg = Registry.from_dir()
    pool = reg._pools.pop(("bsc", ID_BSC))                              # the pool is not in labels/pools.json yet
    node = FakeNode({tx: rec})
    ts = dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc)
    enrich.process_txs(chain, node, conn, reg, [(tx, 133405634, ts)])
    assert _stored(conn) == ([], ["guess"])
    reg.add_pool(pool)                                                  # the owner adds it
    node.calls = 0
    logs = []
    sel = reprocess.build_selection("guess", None, None, None, None, None)
    res = reprocess.reprocess_chain(chain, node, conn, reg, sel, log=logs.append)
    assert res["selected"] == 1 and res["rewritten"] == 1 and node.calls == 2     # the other transaction was not read
    assert _stored(conn) == ([tx[2:]], [])
    assert res["changed"] == {"became_trades": 1, "left_review": 1}
    assert any("guess: 1 -> 0" in l for l in logs)
    again = reprocess.reprocess_chain(chain, node, conn, reg, sel, log=lambda m: None)    # nothing waits any more
    assert again["selected"] == 0 and _stored(conn) == ([tx[2:]], [])


def test_the_review_queue_selector_reads_only_what_waits_for_review(conn):
    chain, reg, node = _guess_setup(conn)
    node.calls = 0
    sel = reprocess.build_selection("guess", None, None, None, None, None)
    res = reprocess.reprocess_chain(chain, node, conn, reg, sel, log=lambda m: None)
    assert res["selected"] == 1 and node.calls == 2
    assert _stored(conn)[1] == ["guess"]                                # same reading: it simply stays in the queue
    assert res["changed"] == {}


def test_a_dry_run_reads_nothing_and_writes_nothing(conn):
    chain, reg, node = _guess_setup(conn)
    reg.add_pool(Pool("ethereum", AA, "Test pool", "uniswap-v2", "v2", token0=ETH_RIZE, token1=USDT))
    node.calls = 0
    logs = []
    res = reprocess.reprocess_chain(chain, node, conn, reg, reprocess.build_selection(None, AA, None, None, None, None),
                                    dry_run=True, log=logs.append)
    assert res["dry_run"] and res["selected"] == 1 and node.calls == 0
    assert _stored(conn) == ([TX_OTHER[2:]], ["guess"]) and any("would re-read" in l for l in logs)


def test_an_explicit_list_and_the_chain_in_it(conn):
    chain, reg, node = _guess_setup(conn)
    here = reprocess.select_txs(conn, chain, reg, reprocess.Selection(txs=[(None, TX_GUESS), ("ethereum", TX_OTHER)]), lambda m: None)
    assert [h for h, *_ in here[0]] == [TX_GUESS, TX_OTHER] and here[1] == {"list": 2}
    elsewhere = reprocess.select_txs(conn, chain, reg, reprocess.Selection(txs=[("bsc", TX_GUESS)]), lambda m: None)
    assert elsewhere[0] == []                                          # named for another chain: not read here
    unknown = reprocess.select_txs(conn, chain, reg, reprocess.Selection(txs=[(None, "0x" + "ee" * 32)]), lambda m: None)
    assert unknown[0] == []


def test_the_address_and_flag_selectors_find_stored_trades(conn):
    chain, reg, node = _guess_setup(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE trades SET router = %s, flags = ARRAY['price-implausible']", (bytes.fromhex(OTHER[2:]),))
    conn.commit()
    by_router = reprocess.select_txs(conn, chain, reg, reprocess.Selection(addresses=[OTHER]), lambda m: None)
    assert [h for h, *_ in by_router[0]] == [TX_OTHER] and by_router[1] == {"address": 1}
    by_flag = reprocess.select_txs(conn, chain, reg, reprocess.Selection(flags=["price-implausible"]), lambda m: None)
    assert [h for h, *_ in by_flag[0]] == [TX_OTHER]
    assert reprocess.select_txs(conn, chain, reg, reprocess.Selection(flags=["usd-implausible"]), lambda m: None)[0] == []


def test_a_pool_id_prefix_finds_the_review_rows_that_mention_it(conn):
    chain, reg, node = _guess_setup(conn)
    with conn.cursor() as cur:
        cur.execute("UPDATE enrich_review SET reason = 'unknown_pool', detail = 'unknown_pool_id:0x32a805e65a3219a7'")
    conn.commit()
    sel = reprocess.Selection(pools=["0x32a805e65a3219a7"])
    assert [h for h, *_ in reprocess.select_txs(conn, chain, reg, sel, lambda m: None)[0]] == [TX_GUESS]
    sel2 = reprocess.Selection(reasons=["unknown_pool"])
    assert [h for h, *_ in reprocess.select_txs(conn, chain, reg, sel2, lambda m: None)[0]] == [TX_GUESS]


# ------------------------------------------------------------------------------------------ the shared lock
def test_the_writer_lock_waits_for_the_unified_run_and_releases(conn):
    from indexer import db
    from indexer.unified import ADVISORY_KEY
    other = db.connect(os.environ["TEST_DATABASE_URL"])
    try:
        with other.cursor() as cur:                                     # a unified run holds the lock
            cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_KEY,))
        with pytest.raises(RuntimeError):
            with enrich.writer_lock(conn, ADVISORY_KEY, wait_seconds=0.1, log=lambda m: None):
                pass
        with other.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_KEY,))
        other.commit()
        with enrich.writer_lock(conn, ADVISORY_KEY, wait_seconds=1, log=lambda m: None):
            with other.cursor() as cur:                                 # while we write, the lock is ours
                cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_KEY,))
                assert cur.fetchone()[0] is False
            other.rollback()
        with other.cursor() as cur:                                     # and it is released afterwards
            cur.execute("SELECT pg_try_advisory_lock(%s)", (ADVISORY_KEY,))
            assert cur.fetchone()[0] is True
    finally:
        other.close()
