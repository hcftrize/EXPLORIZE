"""The market diagnostic commands against a fake node: trade-check on the MetaMask-like sale, classify on a pool."""
from indexer import market_check as mc
from indexer.config import load_chains
from indexer.multicall import MULTICALL3
from tests.test_governance import encode_results, parse_aggregate3, word
from tests.test_receipts import EXEC, POOL_A, POOL_B, RIZE, ROUTER, SAFE, SELLER, USDC, metamask_sell

FACTORY = "0x" + "fa" * 20
TX = "0x" + "c6" * 32


def abi_string(s: str) -> bytes:
    raw = s.encode()
    return word(32) + word(len(raw)) + raw + b"\x00" * (-len(raw) % 32)


class FakeNode:
    def __init__(self):
        self.receipt, *_ = metamask_sell()
        self.codes = {ROUTER: "0x6080", EXEC: "0x6080", POOL_A: "0x6080", POOL_B: "0x6080", SAFE: "0x6080",
                      SELLER: "0x"}

    # --- RpcClient surface
    def get_transaction_receipt(self, tx):
        return self.receipt

    def call(self, method, params, **kw):
        if method == "eth_getTransactionByHash":
            return {"from": SELLER, "to": ROUTER, "value": "0x0"}
        raise AssertionError(method)

    def batch(self, calls):
        assert all(m == "eth_getCode" for m, _ in calls)
        return [self.codes.get(p[0].lower(), "0x") for _, p in calls]

    def eth_call(self, to, data, block="latest"):
        assert to == MULTICALL3
        out = []
        for target, cd in parse_aggregate3(data):
            sel = cd[:4].hex()
            out.append(self.answer(target, sel, cd))
        return encode_results(out)

    def answer(self, target, sel, cd):
        if sel == mc.SEL["symbol"]:
            return (True, abi_string({RIZE: "RIZE", USDC: "USDC"}.get(target, "TKN")))
        if sel == mc.SEL["decimals"]:
            return (True, word(6 if target == USDC else 18))
        if target == POOL_A or target == POOL_B:
            if sel == mc.SEL["token0"]:
                return (True, word(int(RIZE, 16)))
            if sel == mc.SEL["token1"]:
                return (True, word(int(USDC, 16)))
            if sel == mc.SEL["factory"]:
                return (True, word(int(FACTORY, 16)))
            if sel == mc.SEL["fee"]:
                return (True, word(3000))
            if sel == mc.SEL["tickSpacing"] and target == POOL_A:
                return (True, word(200))
            if sel == mc.SEL["getReserves"] and target == POOL_B:
                return (True, word(5 * 10 ** 24) + word(10 ** 10) + word(1))
        if sel == "70a08231":                                      # balanceOf on the RIZE token
            return (True, word(7 * 10 ** 18))
        return (False, b"")


def test_trade_check_prints_verdict_and_roles():
    lines = []
    chain = load_chains()["base"]
    ok = mc.trade_check(FakeNode(), chain, [TX], None, lines.append)
    text = "\n".join(lines)
    assert ok
    assert "VERDICT: SELL" in text
    assert "SOLD 2,000,000 RIZE for 4,344.247 USDC" in text
    assert "Fills (2)" in text
    assert f"{SELLER}  wallet  trader" in text                      # full address, type and role in the legend
    assert f"{ROUTER}  contract  relay" in text


def test_classify_tells_pools_from_wallets_and_groups_by_factory():
    lines = []
    chain = load_chains()["base"]
    ok = mc.classify(FakeNode(), chain, [(POOL_A, 500), (POOL_B, 300), (SELLER, 20), (ROUTER, 10)], None, lines.append)
    text = "\n".join(lines)
    assert ok
    assert "POOL RIZE/USDC  -  concentrated-liquidity pool (tickSpacing 200)" in text
    assert "constant-product style pool (getReserves)" in text
    assert "reserves 5,000,000.00 RIZE / 10,000.00 USDC" in text
    assert "WALLET (no code)" in text
    assert "CONTRACT (not a pool)" in text
    assert f"{FACTORY}: 2 pool(s)" in text
    assert "holds 7 RIZE" in text


def test_a_failed_receipt_lookup_is_reported_not_fatal():
    node = FakeNode()
    node.receipt = None
    lines = []
    ok = mc.trade_check(node, load_chains()["base"], [TX], None, lines.append)
    assert ok is False and "receipt not found" in "\n".join(lines)


def test_a_delegated_wallet_is_a_wallet_not_a_contract():
    node = FakeNode()
    node.codes[SELLER] = "0xef0100" + "ab" * 20                       # EIP-7702 delegation designator
    kinds = mc.code_kinds(node, [SELLER, ROUTER, "0x" + "11" * 20])
    assert kinds[SELLER] == "smart-wallet" and kinds[ROUTER] == "contract" and kinds["0x" + "11" * 20] == "wallet"
    assert mc.is_contract(node, [SELLER])[SELLER] is False
    lines = []
    mc.trade_check(node, load_chains()["base"], [TX], None, lines.append)
    assert f"{SELLER}  smart-wallet  trader" in "\n".join(lines)
