"""Reconcile ONE transaction from its receipt: who really gained / lost what, and what kind of move it was.

Raw RIZE Transfer events show every hop of a route (router -> executor -> pools). The truth of a trade is in
the NET change per address and per token, like a bank statement:

    trader   -2,000,000 RIZE   +4,344.247 USDC      <- the real sell
    pool A   +1,463,114 RIZE   -3,203.458 USDC      <- a fill
    pool B   +  536,885 RIZE   -1,179.137 USDC      <- a fill
    safe     +38.348 USDC                           <- a fee
    router, executor: all zero                      <- relays (plumbing, hidden)

Inputs are the logs of the receipt only: ERC-20 Transfer events, wrapped-native Deposit / Withdrawal events
(WETH9-style tokens emit those instead of Transfer when wrapping), and Uniswap-V2/V3-style Swap events, which
tell us which addresses are pools. Native ETH sent by a router has no log, so a sale paid in ETH shows up on the
pool side (the pool paid out WETH); the report says so.

Pure functions, no network: they are tested against a fake receipt built from a real MetaMask swap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .logs import TRANSFER_TOPIC0

ZERO = "0x" + "00" * 20
DEPOSIT_TOPIC0 = "0xe1fffcc4923d04b559f4d29a8bfc6cda04eb5b0d3c460751c2402c5c5cc9109c"      # Deposit(address,uint256)
WITHDRAWAL_TOPIC0 = "0x7fcf532c15f0a6db0bd6d0e038bea71d30d808c7d98cb3bf7268a95bf5081b65"   # Withdrawal(address,uint256)
SWAP_TOPICS = {
    "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67",   # Uniswap V3 / Slipstream style
    "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822",   # Uniswap V2 / Aerodrome V2 style
    "0x19b47279256b2a23a1665c810c8d55a1758940ee09377d4f8d26497a3577dc83",   # Slipstream variant with 9 fields
}
# Liquidity events emitted by the pool itself
MINT_TOPICS = {
    "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde",   # V3-style Mint (position opened / increased)
    "0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f",   # V2-style Mint
}
BURN_TOPICS = {
    "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c",   # V3-style Burn (liquidity decreased)
    "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496",   # V2-style Burn
}
COLLECT_TOPIC = "0x70935338e69775456a85ddef226c395fb668b63fa0115f5f20610b388e6ca9c0"  # V3-style Collect (fees / tokens taken out)

# Wrapped native token of each chain (WETH on Base and Ethereum, WBNB, WPOL)
WRAPPED_NATIVE = {
    "base": "0x4200000000000000000000000000000000000006",
    "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "bsc": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    "polygon": "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
}
# Used only to price a trade when the counter asset is a dollar stablecoin (matched on the symbol read on-chain)
STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDBC", "USDC.E", "USDT0", "USDS", "FDUSD", "LUSD", "GHO"}


def _addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


@dataclass
class Recon:
    verdict: str                                   # sell | buy | arbitrage | liquidity_add | liquidity_remove | transfer | mint | burn | passthrough | unknown
    note: str = ""
    nets: dict = field(default_factory=dict)       # address -> {token: net raw amount}
    transfers: list = field(default_factory=list)  # (log_index, token, from, to, amount) as emitted
    swap_pools: list = field(default_factory=list)
    traders: list = field(default_factory=list)
    venues: list = field(default_factory=list)
    relays: list = field(default_factory=list)
    fees: list = field(default_factory=list)       # (address, token, amount)
    fills: list = field(default_factory=list)      # {"pool", "rize", "counter": {token: amount}}
    trader_rize: int = 0                           # net RIZE of the traders (negative = sold)
    trader_counter: dict = field(default_factory=dict)  # token -> net amount the traders received (+) / paid (-)
    gross_counter: dict = field(default_factory=dict)   # token -> what the pools exchanged against RIZE
    counter_source: str = ""                       # "trader" (their own net) | "pools" (pool side only)
    swap_count: int = 0                            # Swap events seen in the receipt (any pool)
    lp_pools: dict = field(default_factory=dict)   # pool -> set of {"mint","burn","collect"} seen


def parse_logs(receipt: dict, wrapped: str):
    """-> (nets, transfers, swap_pools, seen, swap_count, lp) from the receipt logs."""
    nets: dict[str, dict[str, int]] = {}
    transfers: list = []
    pools: set[str] = set()
    seen: set[str] = set()
    swaps = 0
    lp: dict[str, set[str]] = {}

    def add(a: str, token: str, delta: int) -> None:
        d = nets.setdefault(a, {})
        d[token] = d.get(token, 0) + delta

    for lg in receipt.get("logs") or []:
        if lg.get("removed"):
            continue
        topics = [t.lower() for t in lg.get("topics") or []]
        if not topics:
            continue
        token = lg["address"].lower()
        data = (lg.get("data") or "0x")[2:]
        idx = int(lg["logIndex"], 16) if isinstance(lg.get("logIndex"), str) else int(lg.get("logIndex") or 0)
        t0 = topics[0]
        if t0 == TRANSFER_TOPIC0 and len(topics) == 3 and len(data) >= 64:      # ERC-20 (ERC-721 has 4 topics)
            frm, to, amount = _addr(topics[1]), _addr(topics[2]), int(data[:64], 16)
            transfers.append((idx, token, frm, to, amount))
            add(frm, token, -amount)
            add(to, token, amount)
            seen.update((frm, to))
        elif t0 == DEPOSIT_TOPIC0 and token == wrapped and len(topics) == 2 and len(data) >= 64:
            dst = _addr(topics[1]); add(dst, token, int(data[:64], 16)); seen.add(dst)
        elif t0 == WITHDRAWAL_TOPIC0 and token == wrapped and len(topics) == 2 and len(data) >= 64:
            src = _addr(topics[1]); add(src, token, -int(data[:64], 16)); seen.add(src)
        elif t0 in SWAP_TOPICS:
            pools.add(token)
            swaps += 1
        elif t0 in MINT_TOPICS:
            lp.setdefault(token, set()).add("mint")
        elif t0 in BURN_TOPICS:
            lp.setdefault(token, set()).add("burn")
        elif t0 == COLLECT_TOPIC:
            lp.setdefault(token, set()).add("collect")
    for a in list(nets):                         # drop zero nets: those addresses are relays
        nets[a] = {t: v for t, v in nets[a].items() if v != 0}
        if not nets[a]:
            del nets[a]
    return nets, transfers, pools, seen, swaps, lp


def reconcile(receipt: dict, rize: str, wrapped: str = "", tx_from: str | None = None) -> Recon:
    rize = rize.lower()
    wrapped = (wrapped or "").lower()
    nets, transfers, pools, seen, swaps, lp = parse_logs(receipt, wrapped)
    r = Recon(verdict="unknown", nets=nets, transfers=sorted(transfers), swap_pools=sorted(pools),
              swap_count=swaps, lp_pools=lp)
    rz = {a: n[rize] for a, n in nets.items() if n.get(rize)}              # parties with a net RIZE change
    others = lambda a: {t: v for t, v in nets.get(a, {}).items() if t != rize}  # noqa: E731
    moved = {a for a in seen if a != ZERO}
    r.relays = sorted(a for a in moved if a not in nets)                    # moved tokens but net zero everywhere

    if not rz:
        if pools and any(v > 0 for a in nets if a not in pools for v in others(a).values()):
            r.verdict, r.note = "arbitrage", "RIZE passes through pools and nets to zero; the profit is in another token"
        else:
            r.verdict, r.note = "passthrough", "RIZE moved but nobody's RIZE balance changed"
        return r
    if ZERO in rz:
        r.verdict = "mint" if rz[ZERO] < 0 else "burn"
        r.traders = sorted(a for a in rz if a != ZERO)
        r.trader_rize = sum(v for a, v in rz.items() if a != ZERO)
        return r

    lp_venues = sorted(a for a in rz if a in lp)
    if not pools and lp_venues:                                  # liquidity events, no swap
        kinds = set().union(*(lp[a] for a in lp_venues))
        r.venues = lp_venues
        r.traders = sorted(a for a in rz if a not in lp)
        r.trader_rize = sum(rz[a] for a in r.traders)
        if "mint" in kinds and "burn" in kinds:
            r.verdict, r.note = "liquidity_rebalance", "liquidity removed and added in the same transaction"
        elif "mint" in kinds:
            r.verdict = "liquidity_add"
        elif "burn" in kinds:
            r.verdict = "liquidity_remove"
        else:
            r.verdict, r.note = "fee_collect", "tokens collected from a position (fees or already-removed liquidity)"
        return r

    venues = sorted(a for a in rz if a in pools)
    if venues:
        sum_v = sum(rz[v] for v in venues)
        traders = sorted(a for a in rz if a not in pools)
        r.venues, r.traders = venues, traders
        if sum_v == 0:
            r.verdict, r.note = "arbitrage", "pools exchanged RIZE with each other; net RIZE zero"
        else:
            r.verdict = "sell" if sum_v > 0 else "buy"
    else:
        # no recognised Swap event: liquidity add/remove, or a swap through a venue we cannot see
        frm = (tx_from or "").lower()
        same = [a for a in rz if a != ZERO and others(a) and all(_sign(v) == _sign(rz[a]) for v in others(a).values())]
        opp = [a for a in rz if a != ZERO and any(_sign(v) == -_sign(rz[a]) for v in others(a).values())]

        def pick(group_neg: list, group_pos: list) -> tuple[list, str]:
            """Which side is the trader / provider? The sender's side, else the smaller side, else the paying side."""
            if frm in group_pos:
                return group_pos, "sender's side"
            if frm in group_neg:
                return group_neg, "sender's side"
            if len(group_pos) != len(group_neg) and group_pos and group_neg:
                return (group_pos if len(group_pos) < len(group_neg) else group_neg), "fewer addresses"
            return group_neg, "default guess"

        if same and not opp:                                     # both RIZE and the other asset move the same way
            neg = [a for a in same if rz[a] < 0]
            pos = [a for a in same if rz[a] > 0]
            if neg and pos:
                tr, why = pick(neg, pos)
                r.traders = sorted(tr)
                r.venues = sorted(a for a in rz if a not in tr)
                r.verdict = "liquidity_add" if rz[tr[0]] < 0 else "liquidity_remove"
                r.note = f"no Swap event; provider chosen by {why}"
            else:
                return r
        elif opp:
            neg = [a for a in opp if rz[a] < 0]
            pos = [a for a in opp if rz[a] > 0]
            tr, why = pick(neg, pos)
            if frm in tr:
                tr = [frm]
            r.traders = sorted(tr)
            r.venues = sorted(a for a in rz if a not in tr)
            r.verdict = "sell" if rz[tr[0]] < 0 else "buy"
            r.note = f"no Swap event recognised: venues guessed from the flows (trader chosen by {why})"
        elif not any(others(a) for a in nets):
            r.traders = sorted(a for a in rz if rz[a] < 0)
            r.venues = sorted(a for a in rz if rz[a] > 0)
            r.verdict = "transfer"
            r.note = "only RIZE moved"
        else:
            return r

    r.trader_rize = sum(rz[a] for a in r.traders)
    for a in r.traders:
        for t, v in others(a).items():
            r.trader_counter[t] = r.trader_counter.get(t, 0) + v
    if r.verdict in ("sell", "buy", "arbitrage"):
        for v in r.venues:
            counter = {t: abs(x) for t, x in others(v).items() if _sign(x) == -_sign(rz[v])}
            r.fills.append({"pool": v, "rize": abs(rz[v]), "counter": counter, "dir": _sign(rz[v])})   # +1: pool received RIZE
            for t, x in counter.items():
                r.gross_counter[t] = r.gross_counter.get(t, 0) + x
        want = 1 if r.verdict == "sell" else -1                 # the traders should RECEIVE the counter on a sell
        seen_by_trader = {t: x for t, x in r.trader_counter.items() if _sign(x) == want}
        r.counter_source = "trader" if seen_by_trader else "pools"
        r.trader_counter = seen_by_trader if seen_by_trader else {}
        known = set(r.traders) | set(r.venues)
        for a, n in nets.items():                                # small leftovers: fees
            if a in known or rz.get(a) or a == ZERO:
                continue
            for t, x in n.items():
                if t != rize and _sign(x) == want:
                    r.fees.append((a, t, x))
    return r


# ----------------------------------------------------------------------------------------------- report
def fmt_amount(raw: int, decimals: int = 18, places: int = 6) -> str:
    d = Decimal(raw) / (Decimal(10) ** decimals)
    s = f"{d:,.{places}f}".rstrip("0").rstrip(".") if places else f"{d:,.0f}"
    return s if s not in ("", "-0") else "0"


def report(r: Recon, rize: str, meta: dict, short=lambda a: a) -> list[str]:
    """Readable lines. meta: token address -> (symbol, decimals)."""
    rize = rize.lower()

    def sym(t: str) -> str:
        return meta.get(t, (t[:8] + "…", 18))[0]

    def amt(t: str, raw: int) -> str:
        return fmt_amount(raw, meta.get(t, ("", 18))[1]) + " " + sym(t)

    out = [f"VERDICT: {r.verdict.upper()}" + (f"   ({r.note})" if r.note else "")]
    out.append("Net change per address (zero nets hidden):")
    for a, n in sorted(r.nets.items(), key=lambda kv: -abs(kv[1].get(rize, 0))):
        role = ("trader" if a in r.traders else "venue/pool" if a in r.venues else
                "pool (Swap event)" if a in r.swap_pools else "other")
        parts = ", ".join(("+" if v > 0 else "-") + amt(t, abs(v)) for t, v in sorted(n.items(), key=lambda kv: kv[0] != rize))
        out.append(f"   {short(a)}  [{role}]  {parts}")
    if r.relays:
        out.append("Relays (moved tokens, net zero, hidden): " + ", ".join(short(a) for a in r.relays))
    if r.verdict in ("sell", "buy", "arbitrage"):
        rz = abs(r.trader_rize)
        paid = r.trader_counter if r.counter_source == "trader" else r.gross_counter
        what = "SOLD" if r.verdict == "sell" else "BOUGHT" if r.verdict == "buy" else "CYCLED"
        out.append(f"{what} {amt(rize, rz)}" + (" for " + " + ".join(amt(t, abs(x)) for t, x in paid.items()) if paid else ""))
        if r.counter_source == "pools" and paid:
            out.append("   (the traders' own counter-asset change is not visible in the logs, e.g. paid in native ETH: "
                       "amount taken from the pool side, shown as the wrapped token)")
        stable = [t for t in paid if sym(t).upper() in STABLE_SYMBOLS]
        if stable and rz:
            t = stable[0]
            dec = meta.get(t, ("", 18))[1]
            net_price = Decimal(abs(paid[t])) / Decimal(10) ** dec / (Decimal(rz) / Decimal(10) ** 18)
            out.append(f"Average price (what the trader got/paid): ${net_price:.6f} per RIZE")
            if r.gross_counter.get(t) and r.counter_source == "trader":
                gross = Decimal(r.gross_counter[t]) / Decimal(10) ** dec / (Decimal(rz) / Decimal(10) ** 18)
                out.append(f"Price at the pools (before fees): ${gross:.6f} per RIZE")
        if r.fees:
            for a, t, x in r.fees:
                base = r.gross_counter.get(t)
                pct = f"  = {Decimal(x) * 100 / Decimal(base):.3f}% of the pool amount" if base else ""
                out.append(f"Fee / other recipient: {short(a)} receives {amt(t, x)}{pct}")
        out.append(f"Fills ({len(r.fills)}):")
        for i, f in enumerate(r.fills, 1):
            c = " + ".join(amt(t, x) for t, x in f["counter"].items())
            out.append(f"   #{i} pool {short(f['pool'])}: {amt(rize, f['rize'])} <-> {c}")
        # Pool-side flow, the way monitoring bots count it: RIZE into pools = sell side, RIZE out of pools = buy side
        out.append(f"Pool-side flow ({r.swap_count} swap event(s) in the receipt):")
        for d, name in ((1, "SELL side (RIZE into pools)"), (-1, "BUY side  (RIZE out of pools)")):
            fs = [f for f in r.fills if f["dir"] == d]
            if not fs:
                continue
            tot = sum(f["rize"] for f in fs)
            cnt: dict = {}
            for f in fs:
                for t, x in f["counter"].items():
                    cnt[t] = cnt.get(t, 0) + x
            line = f"   {name}: {fmt_amount(tot, 18, 18)} RIZE in {len(fs)} pool(s)"
            line += " against " + " + ".join(amt(t, x) for t, x in cnt.items()) if cnt else ""
            st = [t for t in cnt if sym(t).upper() in STABLE_SYMBOLS]
            if st and tot:
                px = Decimal(cnt[st[0]]) / Decimal(10) ** meta.get(st[0], ("", 18))[1] / (Decimal(tot) / Decimal(10) ** 18)
                line += f"  -> ${px:.8f} per RIZE"
            out.append(line)
    elif r.verdict in ("liquidity_add", "liquidity_remove", "liquidity_rebalance", "fee_collect"):
        what = {"liquidity_add": "added", "liquidity_remove": "removed", "liquidity_rebalance": "moved (removed + added)",
                "fee_collect": "collected (fees / tokens)"}[r.verdict]
        out.append(f"Liquidity {what} by: " + ", ".join(short(a) for a in r.traders) + "   in pool(s): " +
                   ", ".join(f"{short(a)} [{'+'.join(sorted(r.lp_pools.get(a, [])))}]" for a in r.venues))
    elif r.verdict in ("transfer", "mint", "burn"):
        out.append("From: " + ", ".join(short(a) for a in r.traders) + "   To: " + ", ".join(short(a) for a in r.venues))
    out.append(f"Raw transfers in the receipt ({len(r.transfers)}), in log order:")
    for idx, t, f, to, x in r.transfers:
        out.append(f"   log {idx:>3}: {short(f)} -> {short(to)}  {amt(t, x)}")
    return out
