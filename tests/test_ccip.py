"""Bridge reconciliation with the Chainlink CCIP API (answers shaped like the real one, fetched for the BNB Chain -> Base burn
0xbdc00dc5...e3fa of 44,070.027728 RIZE)."""
import datetime as dt

from indexer import ccip
from indexer.config import load_chains
from indexer.registry import Registry

SEND = "0xbdc00dc5bb788115d3348e2a1e850b3b4e85eb42e0188f568245feff07a6e3fa"
RECEIPT = "0x2aca8c3de9629313ac8fcb00b00648892d23d897e1af17b1b5608caa421d9cc8"
WALLET = "0xed463eb7f2af44619a2a024b901d278d09542e52"
AMOUNT = 44070027728000000000173
MESSAGE = {
    "messageId": "0x847d8a68a3703b55d5e3765bc936312f662b08f2f5f84408b71ca0c1e4c65058", "status": "SUCCESS",
    "sendTransactionHash": SEND, "receiptTransactionHash": RECEIPT,
    "sourceNetworkInfo": {"chainId": "56", "name": "binance_smart_chain-mainnet"},
    "destNetworkInfo": {"chainId": "8453", "name": "ethereum-mainnet-base-1"},
    "sourceTokenAmount": {"tokenAddress": "0xAEDAff046601BEb063b647845Dfb21841f32d6A4", "tokenAmount": str(AMOUNT)},
    "sender": "0xEd463eb7F2af44619a2a024b901D278D09542e52", "receiver": "0xEd463eb7F2af44619a2a024b901D278D09542e52",
    "sendTimestamp": "2026-11-13T19:12:31.000Z", "receiptTimestamp": "2026-11-13T19:12:36.000Z"}


def put(conn, chain_id, tx, frm, to, amount, block=1):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO transfers (chain_id, block_number, block_time, tx_hash, log_index, from_addr, to_addr, amount)"
                    " VALUES (%s,%s,%s,%s,0,%s,%s,%s)",
                    (chain_id, block, dt.datetime(2026, 11, 13, tzinfo=dt.timezone.utc), bytes.fromhex(tx[2:]),
                     bytes.fromhex(frm[2:]), bytes.fromhex(to[2:]), amount))
    conn.commit()


def test_parse_message_takes_the_rize_amount_and_both_hashes():
    reg = Registry.from_dir()
    row = ccip.parse_message(MESSAGE, {56: reg.rize("bsc"), 8453: reg.rize("base")})
    assert row["amount"] == AMOUNT and row["status"] == "SUCCESS"
    assert (row["source_chain_id"], row["dest_chain_id"]) == (56, 8453)
    assert row["source_tx"].hex() == SEND[2:] and row["dest_tx"].hex() == RECEIPT[2:]
    assert row["sent_at"] < row["received_at"]


def test_a_burn_is_matched_with_its_message_and_its_mint(conn):
    zero = "0x" + "00" * 20
    put(conn, 56, SEND, WALLET, zero, AMOUNT, block=100)                 # the burn
    put(conn, 8453, RECEIPT, zero, WALLET, AMOUNT, block=200)            # the mint on the other chain
    chains = [load_chains()["bsc"], load_chains()["base"]]
    asked = []

    def fetch(url):
        asked.append(url)
        return {"data": [MESSAGE], "pagination": {}}

    reg = Registry.from_dir()
    stats = ccip.ccip_sync(conn, chains, reg, fetch=fetch, pause=0, log=lambda m: None)
    assert stats["burns_checked"] == 1 and stats["messages"] == 1
    assert "sourceTransactionHash=" + SEND in asked[0]
    rep = ccip.bridge_report(conn)
    assert rep["messages"] == 1 and rep["matched"] == 1 and rep["mismatch"] == 0
    ccip.ccip_sync(conn, chains, reg, fetch=fetch, pause=0, log=lambda m: None)   # nothing new to ask
    assert len(asked) == 1


def test_a_burn_without_message_is_remembered_and_a_wrong_mint_is_flagged(conn):
    zero = "0x" + "00" * 20
    other = "0x" + "ab" * 32
    put(conn, 56, other, WALLET, zero, 5 * 10 ** 18, block=50)
    put(conn, 56, SEND, WALLET, zero, AMOUNT, block=100)
    put(conn, 8453, RECEIPT, zero, WALLET, AMOUNT - 1, block=200)         # minted one wei less
    calls = []

    def fetch(url):
        calls.append(url)
        return {"data": [MESSAGE]} if SEND in url else {"data": []}

    reg = Registry.from_dir()
    chains = [load_chains()["bsc"]]
    ccip.ccip_sync(conn, chains, reg, fetch=fetch, pause=0, log=lambda m: None)
    rep = ccip.bridge_report(conn)
    assert rep["mismatch"] == 1 and rep["matched"] == 0 and rep["burns_without_message"] == {56: 1}
    n = len(calls)
    ccip.ccip_sync(conn, chains, reg, fetch=fetch, pause=0, log=lambda m: None)
    assert len(calls) == n


def test_a_message_still_in_flight_is_asked_again_by_id(conn):
    zero = "0x" + "00" * 20
    put(conn, 56, SEND, WALLET, zero, AMOUNT)
    reg = Registry.from_dir()
    chains = [load_chains()["bsc"]]
    pending = {**MESSAGE, "status": "COMMITTED", "receiptTransactionHash": None, "receiptTimestamp": None}
    answers = iter([{"data": [pending]}, MESSAGE])
    urls = []

    def fetch(url):
        urls.append(url)
        return next(answers)

    ccip.ccip_sync(conn, chains, reg, fetch=fetch, pause=0, log=lambda m: None)
    stats = ccip.ccip_sync(conn, chains, reg, fetch=fetch, pause=0, log=lambda m: None)
    assert stats["refreshed"] == 1 and urls[-1].endswith(MESSAGE["messageId"])
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM ccip_messages")
        assert cur.fetchone()[0] == "SUCCESS"
