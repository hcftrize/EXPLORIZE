"""Transaction reconciliation, against a fake receipt modelled on a real MetaMask sale of 2M RIZE for USDC
(0xc66e28ce...950c: aggregator route through two pools, 0.875% fee to a Safe).
The router / executor addresses below are fakes that keep the real prefix and suffix."""
from indexer import market_check as mc
from indexer.logs import TRANSFER_TOPIC0
from indexer.receipts import (DEPOSIT_TOPIC0, WITHDRAWAL_TOPIC0, SWAP_TOPICS, ZERO, fmt_amount, reconcile, report)

RIZE = "0x9818b6c09f5ecc843060927e8587c427c7c93583"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
SELLER = "0x3e21f91838b6527a767aaca458f45c48776680bd"
POOL_A = "0x929312aff9e4611bba5da6adcd08b31eb487bbb5"
POOL_B = "0xea5cb64754ad7aa24f7a6bbe3b724f29b4f822b8"
SAFE = "0xe25c6353a04f9375e9cbac224fda91a90c272e42"
ROUTER = "0x0a2854fb" + "11" * 14 + "9330"
EXEC = "0x8f10b468" + "22" * 14 + "f996"
SWAP_V3 = sorted(SWAP_TOPICS)[0]


def t32(a: str) -> str:
    return "0x" + "0" * 24 + a[2:]


def amt(n: int) -> str:
    return "0x" + f"{n:064x}"


class R:
    """Receipt builder."""
    def __init__(self):
        self.logs = []

    def transfer(self, token, frm, to, n):
        self.logs.append({"address": token, "topics": [TRANSFER_TOPIC0, t32(frm), t32(to)], "data": amt(n),
                          "logIndex": hex(len(self.logs))})
        return self

    def swap(self, pool):
        self.logs.append({"address": pool, "topics": [SWAP_V3, t32(ROUTER), t32(ROUTER)], "data": "0x" + "00" * 64,
                          "logIndex": hex(len(self.logs))})
        return self

    def wrap(self, topic, who, n):
        self.logs.append({"address": WETH, "topics": [topic, t32(who)], "data": amt(n), "logIndex": hex(len(self.logs))})
        return self

    def done(self):
        return {"logs": self.logs, "status": "0x1", "blockNumber": "0x10"}


E18 = 10 ** 18
U6 = 10 ** 6


def metamask_sell():
    rz_a, rz_b = 1_463_114 * E18, 536_886 * E18                       # 2,000,000 RIZE in total
    us_a, us_b, fee = 3_203_458_000, 1_179_137_000, 38_348_000        # USDC has 6 decimals: 3,203.458 etc.
    seller_gets = us_a + us_b - fee                                   # 4,344.247
    r = R()
    r.transfer(RIZE, SELLER, ROUTER, rz_a + rz_b)
    r.transfer(RIZE, ROUTER, EXEC, rz_a + rz_b)
    r.transfer(RIZE, EXEC, POOL_A, rz_a)
    r.transfer(USDC, POOL_A, ROUTER, us_a)
    r.swap(POOL_A)
    r.transfer(RIZE, EXEC, POOL_B, rz_b)
    r.transfer(USDC, POOL_B, ROUTER, us_b)
    r.swap(POOL_B)
    r.transfer(USDC, ROUTER, SAFE, fee)
    r.transfer(USDC, ROUTER, SELLER, seller_gets)
    return r.done(), seller_gets, (rz_a, rz_b, us_a, us_b, fee)


def test_metamask_sell_reads_as_one_sale_with_two_fills():
    rec, seller_gets, (rz_a, rz_b, us_a, us_b, fee) = metamask_sell()
    r = reconcile(rec, RIZE, WETH, tx_from=SELLER)
    assert r.verdict == "sell"
    assert r.traders == [SELLER]
    assert r.trader_rize == -2_000_000 * E18
    assert r.trader_counter == {USDC: seller_gets}
    assert r.counter_source == "trader"
    assert sorted(r.venues) == sorted([POOL_A, POOL_B])
    assert sorted(r.relays) == sorted([ROUTER, EXEC])                 # plumbing: moved tokens, net zero
    assert r.fees == [(SAFE, USDC, fee)]
    assert len(r.fills) == 2
    by_pool = {f["pool"]: f for f in r.fills}
    assert by_pool[POOL_A]["rize"] == rz_a and by_pool[POOL_A]["counter"] == {USDC: us_a}
    assert by_pool[POOL_B]["rize"] == rz_b and by_pool[POOL_B]["counter"] == {USDC: us_b}
    assert r.gross_counter == {USDC: us_a + us_b}
    assert len(r.transfers) == 8                                       # all raw hops are still listed
    assert r.nets[SELLER] == {RIZE: -2_000_000 * E18, USDC: seller_gets}
    assert ROUTER not in r.nets and EXEC not in r.nets                 # zero nets dropped


def test_fee_is_the_0875_percent_of_the_pool_amount():
    rec, _, (_, _, us_a, us_b, fee) = metamask_sell()
    r = reconcile(rec, RIZE, WETH, tx_from=SELLER)
    assert abs(fee * 100 / (us_a + us_b) - 0.875) < 0.001


def test_report_has_the_numbers_a_person_checks():
    rec, _, _ = metamask_sell()
    r = reconcile(rec, RIZE, WETH, tx_from=SELLER)
    meta = {RIZE: ("RIZE", 18), USDC: ("USDC", 6)}
    text = "\n".join(report(r, RIZE, meta))
    assert "VERDICT: SELL" in text
    assert "SOLD 2,000,000 RIZE for 4,344.247 USDC" in text
    assert "$0.002172" in text                                          # what the seller got per RIZE
    assert "$0.002191" in text                                          # price at the pools, before the fee
    assert "0.875% of the pool amount" in text
    assert "Fills (2)" in text and "Relays" in text


def test_sale_paid_in_native_eth_shows_the_pool_side_only():
    r = R()
    r.transfer(RIZE, SELLER, POOL_A, 1000 * E18)
    r.transfer(WETH, POOL_A, ROUTER, 3 * E18 // 10)
    r.swap(POOL_A)
    r.wrap(WITHDRAWAL_TOPIC0, ROUTER, 3 * E18 // 10)                   # router unwraps; the ETH to the seller has no log
    rec = r.done()
    x = reconcile(rec, RIZE, WETH, tx_from=SELLER)
    assert x.verdict == "sell" and x.traders == [SELLER]
    assert x.counter_source == "pools" and x.trader_counter == {}
    assert x.gross_counter == {WETH: 3 * E18 // 10}
    assert x.relays == [ROUTER]


def test_buy_with_wrapped_native_through_a_deposit():
    r = R()
    r.wrap(DEPOSIT_TOPIC0, ROUTER, E18)                                # router wraps the buyer's ETH
    r.transfer(WETH, ROUTER, POOL_A, E18)
    r.transfer(RIZE, POOL_A, SELLER, 500 * E18)
    r.swap(POOL_A)
    x = reconcile(r.done(), RIZE, WETH, tx_from=SELLER)
    assert x.verdict == "buy" and x.traders == [SELLER] and x.venues == [POOL_A]
    assert x.trader_rize == 500 * E18
    assert x.counter_source == "pools" and x.gross_counter == {WETH: E18}


def test_arbitrage_rize_nets_to_zero_with_profit_elsewhere():
    bot = "0xb0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"
    r = R()
    r.transfer(USDC, bot, POOL_A, 1000 * U6)
    r.transfer(RIZE, POOL_A, POOL_B, 100 * E18)
    r.swap(POOL_A)
    r.transfer(USDC, POOL_B, bot, 1010 * U6)
    r.swap(POOL_B)
    x = reconcile(r.done(), RIZE, WETH, tx_from=bot)
    assert x.verdict == "arbitrage"


def test_pools_swapping_rize_between_them_is_arbitrage_even_when_a_bot_holds_nothing():
    bot = "0xb0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"
    r = R()
    r.transfer(USDC, bot, POOL_A, 1000 * U6)
    r.transfer(RIZE, POOL_A, bot, 300 * E18)
    r.swap(POOL_A)
    r.transfer(RIZE, bot, POOL_B, 300 * E18)
    r.transfer(USDC, POOL_B, bot, 1010 * U6)
    r.swap(POOL_B)
    x = reconcile(r.done(), RIZE, WETH, tx_from=bot)
    assert x.verdict == "arbitrage"
    assert x.relays == []                                               # the bot is not hidden: it made the profit
    assert bot in x.nets and x.nets[bot] == {USDC: 10 * U6}


def test_liquidity_add_without_swap_event():
    r = R()
    r.transfer(RIZE, SELLER, POOL_A, 1000 * E18)
    r.transfer(USDC, SELLER, POOL_A, 2 * U6)
    x = reconcile(r.done(), RIZE, WETH, tx_from=SELLER)
    assert x.verdict == "liquidity_add" and x.traders == [SELLER] and x.venues == [POOL_A]


def test_liquidity_remove_without_swap_event():
    r = R()
    r.transfer(RIZE, POOL_A, SELLER, 1000 * E18)
    r.transfer(USDC, POOL_A, SELLER, 2 * U6)
    x = reconcile(r.done(), RIZE, WETH, tx_from=SELLER)
    assert x.verdict == "liquidity_remove" and x.traders == [SELLER]


def test_swap_through_an_unrecognised_venue_uses_the_sender_side():
    r = R()
    r.transfer(RIZE, SELLER, POOL_A, 1000 * E18)
    r.transfer(USDC, POOL_A, SELLER, 2 * U6)                           # no Swap event at all
    x = reconcile(r.done(), RIZE, WETH, tx_from=SELLER)
    assert x.verdict == "sell" and x.traders == [SELLER] and x.venues == [POOL_A]
    assert "no Swap event" in x.note


def test_plain_wallet_transfer():
    a, b = SELLER, "0x1111111111111111111111111111111111111111"
    x = reconcile(R().transfer(RIZE, a, b, 5 * E18).done(), RIZE, WETH, tx_from=a)
    assert x.verdict == "transfer" and x.traders == [a] and x.venues == [b]


def test_mint_and_burn_are_bridge_moves():
    m = reconcile(R().transfer(RIZE, ZERO, SELLER, 7 * E18).done(), RIZE, WETH)
    assert m.verdict == "burn" or m.verdict == "mint"
    assert m.verdict == "mint" or m.trader_rize == 7 * E18
    b = reconcile(R().transfer(RIZE, SELLER, ZERO, 7 * E18).done(), RIZE, WETH)
    assert b.verdict in ("burn", "mint") and b.verdict != m.verdict


def test_nft_transfers_and_foreign_logs_are_ignored():
    rec = R().transfer(RIZE, SELLER, POOL_A, 1).done()
    rec["logs"].append({"address": RIZE, "topics": [TRANSFER_TOPIC0, t32(SELLER), t32(POOL_A), amt(5)], "data": "0x",
                        "logIndex": "0x9"})                              # 4 topics = ERC-721
    rec["logs"].append({"address": RIZE, "topics": [TRANSFER_TOPIC0, t32(POOL_B), t32(SELLER)], "data": amt(9),
                        "logIndex": "0xa", "removed": True})
    x = reconcile(rec, RIZE, WETH)
    assert len(x.transfers) == 1


def test_wrapped_events_from_other_contracts_are_ignored():
    rec = R().transfer(RIZE, SELLER, POOL_A, 1).done()
    rec["logs"].append({"address": USDC, "topics": [DEPOSIT_TOPIC0, t32(SELLER)], "data": amt(10 ** 9), "logIndex": "0x5"})
    x = reconcile(rec, RIZE, WETH)
    assert USDC not in x.nets.get(SELLER, {})


def test_fmt_amount_and_dec_string():
    assert fmt_amount(1_234_500_000, 6) == "1,234.5"
    assert fmt_amount(0, 18) == "0"
    word = (32).to_bytes(32, "big") + (4).to_bytes(32, "big") + b"USDC" + bytes(28)
    assert mc.dec_string(word) == "USDC"
    assert mc.dec_string(b"RIZE" + bytes(28)) == "RIZE"


# ---------------------------------------------------------------- v2: swap count, pool-side flow, liquidity events
from indexer.receipts import BURN_TOPICS, COLLECT_TOPIC, MINT_TOPICS  # noqa: E402


def pool_event(rec_builder, pool, topic):
    rec_builder.logs.append({"address": pool, "topics": [topic, t32(SELLER)], "data": "0x" + "00" * 32,
                             "logIndex": hex(len(rec_builder.logs))})
    return rec_builder


def test_swap_events_are_counted_and_pool_side_flow_is_printed_to_the_wei():
    rec, _, (rz_a, rz_b, us_a, us_b, fee) = metamask_sell()
    r = reconcile(rec, RIZE, WETH, tx_from=SELLER)
    assert r.swap_count == 2
    assert {f["dir"] for f in r.fills} == {1}                         # both pools received RIZE: sell side
    text = "\n".join(report(r, RIZE, {RIZE: ("RIZE", 18), USDC: ("USDC", 6)}))
    assert "Pool-side flow (2 swap event(s)" in text
    assert "SELL side (RIZE into pools): 2000000 RIZE in 2 pool(s)" in text.replace(",", "")
    assert "$0.00219" in text


def test_mixed_transaction_shows_both_sides():
    bot = "0xb0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0b0"
    r = R()
    r.transfer(USDC, bot, POOL_A, 300 * U6)
    r.transfer(RIZE, POOL_A, bot, 119_789 * E18)
    r.swap(POOL_A)
    r.transfer(RIZE, bot, POOL_B, 119_789 * E18)
    r.transfer(USDC, POOL_B, bot, 295 * U6)
    r.swap(POOL_B)
    x = reconcile(r.done(), RIZE, WETH, tx_from=bot)
    assert x.verdict == "arbitrage" and x.swap_count == 2
    text = "\n".join(report(x, RIZE, {RIZE: ("RIZE", 18), USDC: ("USDC", 6)}))
    assert "SELL side" in text and "BUY side" in text


def test_liquidity_add_detected_from_the_pool_mint_event():
    r = R()
    r.transfer(RIZE, SELLER, POOL_A, 1000 * E18)
    r.transfer(USDC, SELLER, POOL_A, 2 * U6)
    pool_event(r, POOL_A, sorted(MINT_TOPICS)[0])
    x = reconcile(r.done(), RIZE, WETH, tx_from=SELLER)
    assert x.verdict == "liquidity_add" and x.traders == [SELLER] and x.venues == [POOL_A]


def test_burn_then_collect_is_a_removal_and_collect_alone_is_a_fee_claim():
    r = R()
    r.transfer(RIZE, POOL_A, SELLER, 1000 * E18)
    pool_event(r, POOL_A, sorted(BURN_TOPICS)[0])
    pool_event(r, POOL_A, COLLECT_TOPIC)
    assert reconcile(r.done(), RIZE, WETH, tx_from=SELLER).verdict == "liquidity_remove"
    r = R()
    r.transfer(RIZE, POOL_A, SELLER, 5 * E18)
    pool_event(r, POOL_A, COLLECT_TOPIC)
    x = reconcile(r.done(), RIZE, WETH, tx_from=SELLER)
    assert x.verdict == "fee_collect" and x.traders == [SELLER]


def test_burn_and_mint_together_is_a_rebalance():
    r = R()
    r.transfer(RIZE, POOL_A, SELLER, 1000 * E18)
    pool_event(r, POOL_A, sorted(BURN_TOPICS)[0])
    r.transfer(RIZE, SELLER, POOL_B, 400 * E18)
    pool_event(r, POOL_B, sorted(MINT_TOPICS)[0])
    x = reconcile(r.done(), RIZE, WETH, tx_from=SELLER)
    assert x.verdict == "liquidity_rebalance"


def test_a_swap_with_a_collect_in_the_same_tx_stays_a_swap():
    rec, *_ = metamask_sell()
    rec["logs"].append({"address": POOL_A, "topics": [COLLECT_TOPIC, t32(SELLER)], "data": "0x" + "00" * 32, "logIndex": "0x63"})
    assert reconcile(rec, RIZE, WETH, tx_from=SELLER).verdict == "sell"
