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


def indexed(conn, *ids):
    """The chains the explorer indexes (a message to any other network can only have its burn checked)."""
    reg = Registry.from_dir()
    slug = {56: "bsc", 8453: "base", 1: "ethereum", 137: "polygon"}
    with conn.cursor() as cur:
        for i in ids:
            cur.execute("INSERT INTO chain_state (chain_id, slug, token, start_block, last_block) VALUES (%s,%s,%s,1,1000) "
                        "ON CONFLICT DO NOTHING", (i, slug[i], bytes.fromhex(reg.rize(slug[i])[2:])))
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
    indexed(conn, 56, 8453)
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
    indexed(conn, 56, 8453)
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
    assert rep["mismatch"] == 1 and rep["matched"] == 0 and rep["no_message_found"] == {56: 1}
    assert rep["reasons"] == {"mint below": 1}
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


def test_a_message_to_a_network_without_a_number_does_not_crash_and_is_burn_only(conn):
    zero = "0x" + "00" * 20
    other = dict(MESSAGE, destNetworkInfo={"chainId": "MainNet", "name": "some-other-mainnet"}, receiptTransactionHash=None)
    row = ccip.parse_message(other, {56: Registry.from_dir().rize("bsc")})
    assert row["dest_chain_id"] == 0 and row["raw"]["destNetworkInfo"]["name"] == "some-other-mainnet"
    put(conn, 56, SEND, WALLET, zero, AMOUNT, block=100)
    indexed(conn, 56)
    reg = Registry.from_dir()
    ccip.ccip_sync(conn, [load_chains()["bsc"]], reg, fetch=lambda url: {"data": [other]}, pause=0, log=lambda m: None)
    rep = ccip.bridge_report(conn)
    assert rep["messages"] == 1 and rep["one_side_only_other_network"] == 1 and rep["mismatch"] == 0


def _msg(conn, mid, src, dst, stx, dtx, amount):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ccip_messages (message_id, status, source_chain_id, dest_chain_id, source_tx, dest_tx, amount, sent_at)"
                    " VALUES (%s,'SUCCESS',%s,%s,%s,%s,%s,now())",
                    (bytes.fromhex(mid[2:]), src, dst, bytes.fromhex(stx[2:]) if stx else None,
                     bytes.fromhex(dtx[2:]) if dtx else None, amount))
    conn.commit()


def test_two_messages_executed_in_one_transaction_share_one_mint_and_are_not_flagged(conn):
    zero = "0x" + "00" * 20
    indexed(conn, 56, 8453)
    s1, s2, mint = "0x" + "11" * 32, "0x" + "22" * 32, "0x" + "33" * 32
    put(conn, 8453, s1, WALLET, zero, 54 * 10 ** 18, block=1)
    put(conn, 8453, s2, WALLET, zero, 41 * 10 ** 18, block=2)
    put(conn, 56, mint, zero, WALLET, 95 * 10 ** 18, block=3)            # one mint for both messages
    _msg(conn, "0x" + "a1" * 32, 8453, 56, s1, mint, 54 * 10 ** 18)
    _msg(conn, "0x" + "a2" * 32, 8453, 56, s2, mint, 41 * 10 ** 18)
    rep = ccip.bridge_report(conn)
    assert rep["matched"] == 2 and rep["mismatch"] == 0
    # a third message that was never found leaves the sum short: now the batch is flagged
    put(conn, 56, "0x" + "44" * 32, zero, WALLET, 1, block=4)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("UPDATE transfers SET amount = %s WHERE tx_hash = %s AND chain_id = 56", (96 * 10 ** 18, bytes.fromhex(mint[2:])))
    conn.commit()
    rep = ccip.bridge_report(conn)
    assert rep["mismatch"] == 2 and rep["reasons"] == {"mint above": 2}      # more minted than the messages we know of


def test_a_message_from_canton_without_amount_is_checked_on_the_mint_side_only(conn):
    zero = "0x" + "00" * 20
    indexed(conn, 8453)
    mint = "0x" + "55" * 32
    put(conn, 8453, mint, zero, WALLET, 7 * 10 ** 18, block=5)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ccip_messages (message_id, status, source_chain_id, dest_chain_id, dest_tx, amount) "
                    "VALUES (%s,'SUCCESS',0,8453,%s,NULL)", (bytes.fromhex("b1" * 32), bytes.fromhex(mint[2:])))
    conn.commit()
    rep = ccip.bridge_report(conn)
    assert rep["one_side_only_other_network"] == 1 and rep["mismatch"] == 0


def test_a_transaction_missing_from_our_transfers_is_a_data_gap_not_a_wrong_amount(conn):
    zero = "0x" + "00" * 20
    indexed(conn, 56, 8453)
    s, d = "0x" + "66" * 32, "0x" + "77" * 32
    put(conn, 8453, s, WALLET, zero, 3 * 10 ** 18, block=1)              # the burn is there, the mint transaction is not
    _msg(conn, "0x" + "c1" * 32, 8453, 56, s, d, 3 * 10 ** 18)
    rep = ccip.bridge_report(conn)
    assert rep["not_in_transfers"] == 1 and rep["mismatch"] == 0 and rep["matched"] == 0
    assert rep["reasons"] == {"not_in_transfers: dest": 1}


def test_a_transaction_without_any_burn_or_mint_in_it_is_named_as_such(conn):
    zero = "0x" + "00" * 20
    indexed(conn, 56, 8453)
    s, d = "0x" + "88" * 32, "0x" + "99" * 32
    put(conn, 8453, s, WALLET, "0x" + "cd" * 20, 3 * 10 ** 18, block=1)  # a plain transfer, not a burn
    put(conn, 56, d, zero, WALLET, 3 * 10 ** 18, block=2)
    _msg(conn, "0x" + "c2" * 32, 8453, 56, s, d, 3 * 10 ** 18)
    rep = ccip.bridge_report(conn)
    assert rep["reasons"] == {"burn none": 1} and rep["mismatch"] == 1


def test_a_message_from_a_network_without_amount_but_a_burn_side_is_named(conn):
    zero = "0x" + "00" * 20
    indexed(conn, 8453)
    s = "0x" + "ee" * 32
    put(conn, 8453, s, WALLET, zero, 3 * 10 ** 18, block=1)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO ccip_messages (message_id, status, source_chain_id, dest_chain_id, source_tx, amount) "
                    "VALUES (%s,'SUCCESS',8453,0,%s,NULL)", (bytes.fromhex("b2" * 32), bytes.fromhex(s[2:])))
    conn.commit()
    rep = ccip.bridge_report(conn)
    assert rep["reasons"] == {"burn side without amount": 1}


def test_the_report_describes_the_stored_messages(conn, capsys):
    zero = "0x" + "00" * 20
    indexed(conn, 56, 8453)
    s, d = "0x" + "12" * 32, "0x" + "34" * 32
    put(conn, 8453, s, WALLET, zero, 3 * 10 ** 18, block=1)
    put(conn, 56, d, zero, WALLET, 3 * 10 ** 18, block=2)
    _msg(conn, "0x" + "c3" * 32, 8453, 56, s, d, 3 * 10 ** 18)
    info = ccip.dataset_summary(conn)
    assert info["messages"] == 1 and info["with_amount"] == 1 and info["recent"] == 1
    assert "stored messages: 1" in capsys.readouterr().out


class FakeClient:
    def __init__(self, receipts):
        self.receipts = receipts

    def get_transaction_receipt(self, tx):
        return self.receipts.get(tx)


def _log(token, frm, to):
    from indexer.logs import TRANSFER_TOPIC0
    pad = lambda a: "0x" + a[2:].rjust(64, "0")  # noqa: E731
    return {"address": token, "topics": [TRANSFER_TOPIC0, pad(frm), pad(to)], "data": "0x" + "00" * 31 + "01"}


def test_gap_check_tells_a_hole_in_our_data_from_a_transaction_without_rize(conn):
    zero = "0x" + "00" * 20
    indexed(conn, 56, 8453)
    reg = Registry.from_dir()
    rize_bsc = reg.rize("bsc")
    hole_tx, other_tx, mint = "0x" + "a0" * 32, "0x" + "a1" * 32, "0x" + "a2" * 32
    put(conn, 8453, mint, zero, WALLET, 1, block=7)                      # a mint we do hold, so this side is fine
    _msg(conn, "0x" + "d1" * 32, 56, 8453, hole_tx, mint, 5)             # burn on BSC missing from our transfers
    _msg(conn, "0x" + "d2" * 32, 56, 8453, other_tx, mint, 6)
    clients = {
        56: FakeClient({
            hole_tx: {"blockNumber": hex(1000), "logs": [_log(rize_bsc, WALLET, zero)]},
            other_tx: {"blockNumber": hex(2000), "logs": [_log("0x" + "bb" * 20, WALLET, zero)]}}),
        8453: FakeClient({})}
    out = ccip.gap_check(conn, clients, reg, limit=10, log=lambda m: None)
    assert out["candidates"] == 2 and out["checked"] == 2
    assert out["verdicts"] == {"HOLE: receipt has RIZE transfers we lack": 1, "no RIZE transfer in this transaction": 1}
    assert out["holes"] == {56: [1000]}
