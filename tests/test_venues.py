"""Singleton pools (Uniswap V4, PancakeSwap Infinity) and the RFQ contract, against receipts rebuilt from real
transactions of the runs: amounts are the real ones (the events carry them to the wei); some middle addresses are fakes."""
from indexer.logs import TRANSFER_TOPIC0
from indexer.receipts import reconcile
from indexer.registry import NATIVE, Registry
from indexer.venues import (INFINITY_SWAP, MODIFY_LIQUIDITY, RFQ_FILL, UNISWAP_V4_SWAP, decode_venues, refine)

REG = Registry.from_dir()

# --- Ethereum
ETH_RIZE = "0x9f1e8f87c6321b84bad7dda7dfb86d5115a47605"
USDT = "0xdac17f958d2ee523a2206206994597c13d831ec7"
MANAGER = "0x000000000004444c5dc75cb358380d2e3de08a90"
MM = "0x2b512a198b255f0f55d58f974a989c257bdda258"
ID_ETH_RIZE = "0xbd43b45bda23c14d6917224b709f8eb391bae52158e46613c718597dbe3aa2fe"
RELAY = "0x74de5d4fcbf63e00296fd95d33236b9794016631"
RELAY2 = "0x4fe93ebc4ce6ae4f81601cc7ce7139023919e003"
WALLET = "0xdf820fc62952b5b4ef44ff1978431765603e8c78"
SMART = "0x35940ec3372fabeb52384c997967658ba233dfb4"
FEE_TO = "0x0e9902e50b41bf0099b8ef91ca7488e969c8c3f8"
WETH_ETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"

# --- BNB Chain
BSC_RIZE = "0xaedaff046601beb063b647845dfb21841f32d6a4"
BSC_USDC = "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d"
VAULT = "0x238a358808379702088667322f80ac48bad5e6c4"
CLMANAGER = "0xa0ffb9c1ce1fe56963b0321b32e7a0302114058b"
ID_BSC = "0x465bd99ae2a0bfae8efac4dc88980d29e4e4c390773d1986cf8dc3087fac2340"
BUYER = "0xed463eb7f2af44619a2a024b901d278d09542e52"
ROUTER = "0xd9c500dff816a1da21a48a732d3498bf09dc9aeb"
FREN_POOL = "0x7f471a684bab236597d44caa32fc1afe0b262bea"
V2_SWAP = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"


def t32(x: str) -> str:
    return "0x" + "0" * 24 + x[2:]


def w(n: int) -> str:
    return f"{n & ((1 << 256) - 1):064x}"


class Rec:
    def __init__(self):
        self.logs = []

    def _add(self, addr, topics, data=""):
        self.logs.append({"address": addr, "topics": topics, "data": "0x" + data, "logIndex": hex(len(self.logs))})
        return self

    def transfer(self, token, a, b, n):
        return self._add(token, [TRANSFER_TOPIC0, t32(a), t32(b)], w(n))

    def v4_swap(self, emitter, topic, pool_id, a0, a1, liq=10 ** 20, tick=133642, fee=200800, extra=()):
        return self._add(emitter, [topic, pool_id, t32(RELAY)], "".join(w(x) for x in (a0, a1, 2 ** 90, liq, tick, fee, *extra)))

    def modify(self, emitter, pool_id, lo, hi, delta, salt):
        return self._add(emitter, [MODIFY_LIQUIDITY, pool_id, t32(RELAY)], "".join(w(x) for x in (lo, hi, delta, salt)))

    def rfq(self, token_a, token_b, amt_a, amt_b):
        return self._add(MM, [RFQ_FILL], "".join(w(x) for x in (int(token_a, 16), int(token_b, 16), amt_a, amt_b, 1, 2)))

    def v2_swap(self, pool, a0in, a1in, a0out, a1out):
        return self._add(pool, [V2_SWAP, t32(ROUTER), t32(ROUTER)], "".join(w(x) for x in (a0in, a1in, a0out, a1out)))

    def done(self):
        return {"logs": self.logs, "status": "0x1", "blockNumber": "0x10"}


def run(rec, chain, frm=None):
    rize = REG.rize(chain)
    vi = decode_venues(rec, chain, REG)
    r = reconcile(rec, rize, REG.wrapped(chain), tx_from=frm, venue_addrs=vi.venue_addrs, lp_extra=vi.lp_extra)
    return refine(r, vi, chain, REG, rize), vi


def test_the_registry_reads_the_labels_files():
    assert len(REG.pools("base")) == 13                                  # 7 Aerodrome + Aero, 5 Uniswap, 1 SushiSwap (first list 2026-11-24)
    assert REG.is_not_pool("base", "0x0851e36d477577cc0f7ec4f796a89213bd83e2bc")      # an Arrakis proxy, not a pool
    assert REG.pool("ethereum", ID_ETH_RIZE).kind == "v4"
    assert REG.pool("bsc", ID_BSC).holder_addr == VAULT                  # Pancake: the Vault holds the tokens
    assert REG.pool("ethereum", ID_ETH_RIZE).holder_addr == MANAGER
    assert REG.pool("base", "0x284c507b2d8563660acbe4288963f940c98d7ede") is None     # an Arkham "Proxy", not a pool
    assert REG.label("base", "0x284c507b2d8563660acbe4288963f940c98d7ede") == "Proxy (EIP-1967 Beacon)"
    assert REG.label("base", "0x121fd4ccc615ef36305a9ccc3f0ff6611a5e7759") == "PoolManager (Proxy)"
    assert BSC_USDC in REG.stables("bsc") and USDT in REG.stables("ethereum")
    assert REG.token_meta("bsc")[BSC_USDC] == ("USDC", 18)               # 18 decimals on BNB Chain


def test_uniswap_v4_buy_paid_in_native_eth_is_read_from_the_swap_event():
    """0xd206ca58...: the manager sent 1,979.418 RIZE, the buyer paid 0.003835 ETH, which leaves no token log."""
    rz = 1979418470740360193627
    rec = (Rec().transfer(ETH_RIZE, MANAGER, RELAY, rz).transfer(ETH_RIZE, RELAY, WALLET, rz)
           .v4_swap(MANAGER, UNISWAP_V4_SWAP, ID_ETH_RIZE, -3835182910893151, rz).done())
    r, _ = run(rec, "ethereum", frm=WALLET)
    assert r.verdict == "buy" and r.evidence == "events"
    assert r.traders == [WALLET] and r.trader_rize == rz
    assert len(r.fills) == 1
    f = r.fills[0]
    assert f["pool"] == ID_ETH_RIZE and f["rize"] == rz and f["dir"] == -1
    assert f["counter"] == {NATIVE: 3835182910893151}                    # the native leg, exact, from the event
    assert r.venue_checks == [(MANAGER, -rz, -rz, True)]
    assert r.flags == []


def test_mixed_sale_split_between_uniswap_v4_and_the_rfq_market_maker():
    """0x37a277...: 10,527.68 RIZE sold by a smart account: 7,090.53 into V4 (for ETH), 3,331.88 to the market maker (for USDT),
    1% skimmed in RIZE by an integrator. The old guess called this a BUY."""
    total = 10527684700306075021198
    v4, rfq = 7090530821127693456780, 3331877032175320814205
    fee = total - v4 - rfq
    rec = (Rec().transfer(ETH_RIZE, SMART, RELAY2, total).transfer(ETH_RIZE, RELAY2, FEE_TO, fee)
           .transfer(ETH_RIZE, RELAY2, "0xa43d5d5b259d8a557f14b14f87a4f61293a47112", total - fee)
           .transfer(ETH_RIZE, "0xa43d5d5b259d8a557f14b14f87a4f61293a47112", MANAGER, v4)
           .transfer(ETH_RIZE, "0xa43d5d5b259d8a557f14b14f87a4f61293a47112", RELAY, rfq)
           .transfer(ETH_RIZE, RELAY, MM, rfq)
           .transfer(USDT, MM, "0xa43d5d5b259d8a557f14b14f87a4f61293a47112", 7551747)
           .v4_swap(MANAGER, UNISWAP_V4_SWAP, ID_ETH_RIZE, 8009527305970785, -v4)
           .rfq(ETH_RIZE, USDT, rfq, 7551747).done())
    r, _ = run(rec, "ethereum")
    assert r.verdict == "sell" and r.evidence == "events"
    assert r.traders == [SMART]
    assert r.rize_fees == [(FEE_TO, fee)]
    assert r.pool_rize == v4 + rfq
    pools = {f["pool"]: f for f in r.fills}
    assert pools[ID_ETH_RIZE]["counter"] == {NATIVE: 8009527305970785} and pools[ID_ETH_RIZE]["dir"] == 1
    assert pools[MM]["counter"] == {USDT: 7551747} and pools[MM]["rize"] == rfq
    assert r.gross_counter == {NATIVE: 8009527305970785, USDT: 7551747}
    assert all(ok for *_, ok in r.venue_checks) and r.flags == []


def test_pancake_infinity_buy_with_usdc_18_decimals():
    """0xa61ecda5...: 100 USDC bought 44,070.027728 RIZE; the Vault holds the tokens, the CLPoolManager emits the Swap."""
    rz, usdc = 44070027728000000000173, 99999999999378361648
    rec = (Rec().transfer(BSC_USDC, BUYER, ROUTER, usdc).transfer(BSC_USDC, ROUTER, VAULT, usdc)
           .transfer(BSC_RIZE, VAULT, ROUTER, rz).transfer(BSC_RIZE, ROUTER, BUYER, rz)
           .v4_swap(CLMANAGER, INFINITY_SWAP, ID_BSC, -usdc, rz, extra=(3299,)).done())
    r, vi = run(rec, "bsc", frm=BUYER)
    assert vi.venue_addrs == {VAULT}                                     # the holder, not the manager
    assert r.verdict == "buy" and r.traders == [BUYER]
    f = r.fills[0]
    assert f["pool"] == ID_BSC and f["rize"] == rz and f["counter"] == {BSC_USDC: usdc} and f["dir"] == -1
    assert r.venue_checks[0][3] is True


def test_pancake_sell_into_the_pool_is_a_sell():
    """0xb0b8c94d...: a wallet sold 335.645 RIZE through an aggregator; the swap event shows the swapper PAYING RIZE."""
    rz, usdc = 335645180599796150719, 843480921886032906
    seller = "0x2d9723ed2448a4df7e2689d2385b797d5fdcd1bb"
    rec = (Rec().transfer(BSC_RIZE, seller, ROUTER, rz).transfer(BSC_RIZE, ROUTER, VAULT, rz)
           .transfer(BSC_USDC, VAULT, ROUTER, usdc).transfer(BSC_USDC, ROUTER, seller, usdc)
           .v4_swap(CLMANAGER, INFINITY_SWAP, ID_BSC, usdc, -rz, extra=(3299,)).done())
    r, _ = run(rec, "bsc", frm=seller)
    assert r.verdict == "sell" and r.traders == [seller] and r.fills[0]["dir"] == 1


def test_a_round_trip_through_pancake_and_a_third_party_pool_is_an_arbitrage():
    """0x4ea6ad02...: RIZE bought out of the Infinity pool, sold into a FREN/RIZE pool: net RIZE zero."""
    rz = 173492870397716955831
    other = "0x854f6276e266c1e4b0e54ea1a0474a7e1dc3c1d7"
    rec = (Rec().transfer(BSC_USDC, other, VAULT, 358862889175955949).transfer(BSC_RIZE, VAULT, FREN_POOL, rz)
           .v4_swap(CLMANAGER, INFINITY_SWAP, ID_BSC, -358862889175955949, rz, extra=(3299,))
           .v2_swap(FREN_POOL, 0, rz, 2662615000000000000, 0).done())
    r, _ = run(rec, "bsc")
    assert r.verdict == "arbitrage" and r.evidence == "events"
    assert {f["dir"] for f in r.fills} == {1, -1}


def test_a_swap_in_an_unregistered_singleton_pool_is_flagged_not_guessed():
    rz = 5 * 10 ** 18
    rec = (Rec().transfer(ETH_RIZE, MANAGER, WALLET, rz)
           .v4_swap(MANAGER, UNISWAP_V4_SWAP, "0x" + "ab" * 32, -1, rz).done())
    r, vi = run(rec, "ethereum")
    assert any(f.startswith("unknown_pool_id") for f in r.flags)
    assert vi.venue_addrs == set()                                       # nothing is attributed to a pool we do not know


def test_liquidity_collected_then_re_added_by_a_position_manager():
    """0xded4acf8... (delta 0 = collect the fees of a position) then 0xe4206659... (+liquidity): RIZE 7,382.4793."""
    amount = 7382479300000000000000
    collect = (Rec().transfer(ETH_RIZE, MANAGER, "0x0bfb16be5ce33c8dc9bb9cce3d3bad039a4e42e1", amount)
               .modify(MANAGER, ID_ETH_RIZE, 98000, 138000, 0, 401005).done())
    r, _ = run(collect, "ethereum")
    assert r.verdict == "fee_collect"
    assert r.liquidity == [{"pool": ID_ETH_RIZE, "log": 1, "tick_lower": 98000, "tick_upper": 138000, "delta": 0,
                            "salt": 401005, "kind": "collect", "rize": -amount, "counter": {}}]
    add = (Rec().transfer(ETH_RIZE, "0x0bfb16be5ce33c8dc9bb9cce3d3bad039a4e42e1", MANAGER, amount)
           .modify(MANAGER, ID_ETH_RIZE, 98000, 138000, 10510650574393623351, 401005).done())
    r, _ = run(add, "ethereum")
    assert r.verdict == "liquidity_add" and r.liquidity[0]["kind"] == "add" and r.liquidity[0]["rize"] == amount


def test_negative_ticks_and_negative_liquidity_decode_as_signed():
    rec = Rec().transfer(ETH_RIZE, MANAGER, WALLET, 10 ** 18).modify(MANAGER, ID_ETH_RIZE, -277735, -277635, -5, 1).done()
    r, _ = run(rec, "ethereum")
    lq = r.liquidity[0]
    assert (lq["tick_lower"], lq["tick_upper"], lq["delta"], lq["kind"]) == (-277735, -277635, -5, "remove")


def test_a_swap_event_that_disagrees_with_the_transfers_raises_a_flag():
    rz = 10 * 10 ** 18
    rec = (Rec().transfer(ETH_RIZE, MANAGER, WALLET, rz)
           .v4_swap(MANAGER, UNISWAP_V4_SWAP, ID_ETH_RIZE, -10, rz - 1).done())
    r, _ = run(rec, "ethereum")
    assert any(f.startswith("events_vs_transfers") for f in r.flags)
