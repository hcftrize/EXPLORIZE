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

Beyond the net table the module separates the roles around a trade: the seller / buyer, the pools that filled it,
RIZE fee recipients (integrators skimming RIZE on a sell), second-hop swaps (pool proceeds swapped again, e.g. USDC
to WETH to native ETH), the address that received the proceeds when it is not the seller, and the profit of a round
trip between two pools. Swap / Mint / Burn / Collect events are decoded so their amounts can be checked against
the token movements.

Pure functions, no network: they are tested against fake receipts modelled on real transactions.
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
V2_SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
V2_MINT_TOPIC = "0x4c209b5fc8ad50758f13e2e1088ba56a560dff690a1c6fef26394f4c03821c4f"
V2_BURN_TOPIC = "0xdccd412f0b1252819cb1fd330b93224ca42612892bb3f4f789976e6d81936496"

# Wrapped native token of each chain (WETH on Base and Ethereum, WBNB, WPOL)
WRAPPED_NATIVE = {
    "base": "0x4200000000000000000000000000000000000006",
    "ethereum": "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
    "bsc": "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
    "polygon": "0x0d500b1d8e8ef31e21c99d1db9a6444d3adf1270",
}
# Dollar stablecoins, by ADDRESS (a symbol can be copied by anyone: impostor tokens named "RIZE" exist on Base).
# Only these are used to price a trade in dollars. Other chains: to be filled once confirmed.
STABLES = {
    "base": {
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",   # USDC
        "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2",   # USDT
    },
}
# Fallback used only when no address list is given (tests, quick experiments)
STABLE_SYMBOLS = {"USDC", "USDT", "DAI", "USDBC", "USDC.E", "USDT0", "USDS", "FDUSD", "LUSD", "GHO"}


def _addr(topic: str) -> str:
    return "0x" + topic[-40:].lower()


def _sign(n: int) -> int:
    return (n > 0) - (n < 0)


def _s256(n: int) -> int:
    return n - (1 << 256) if n >= (1 << 255) else n


@dataclass
class Recon:
    verdict: str                                   # sell | buy | arbitrage | liquidity_* | fee_collect | transfer | mint | burn | passthrough | unknown
    note: str = ""
    nets: dict = field(default_factory=dict)       # address -> {token: net raw amount}
    transfers: list = field(default_factory=list)  # (log_index, token, from, to, amount) as emitted
    swap_pools: list = field(default_factory=list)
    traders: list = field(default_factory=list)    # sellers / buyers / liquidity providers
    venues: list = field(default_factory=list)     # pools that filled the trade
    relays: list = field(default_factory=list)     # moved tokens, net zero
    fees: list = field(default_factory=list)       # (address, token, amount): small recipients of the counter asset
    fills: list = field(default_factory=list)      # {"pool", "rize", "counter": {token: amount}, "dir": +1 pool received RIZE}
    trader_rize: int = 0                           # RIZE the traders gave (-) or received (+)
    pool_rize: int = 0                             # net RIZE of the pools: + sold into pools, - bought from pools
    trader_counter: dict = field(default_factory=dict)  # token -> net amount the traders received (sell) / paid (buy)
    gross_counter: dict = field(default_factory=dict)   # token -> what the pools exchanged against RIZE
    counter_source: str = ""                       # "trader" (their own net) | "pools" (pool side only)
    swap_count: int = 0                            # Swap events seen in the receipt (any pool)
    swaps: list = field(default_factory=list)      # (pool, log_index, net0, net1) from the pool's side
    lp_pools: dict = field(default_factory=dict)   # pool -> set of {"mint","burn","collect"} seen
    lp_events: list = field(default_factory=list)  # (pool, log_index, kind, amount0, amount1)
    rize_fees: list = field(default_factory=list)  # (address, amount): RIZE skimmed on a sell / buy
    hops: list = field(default_factory=list)       # addresses doing a second swap on the proceeds
    payees: list = field(default_factory=list)     # (address, token, amount, share): got most of the pool proceeds
    payers: list = field(default_factory=list)     # (address, token, amount, share): paid most of a buy
    profit: dict = field(default_factory=dict)     # arbitrage: token -> gross gain
    native_in: int = 0                             # wrapped native created (Deposit events)
    native_out: int = 0                            # wrapped native unwrapped to native coin (Withdrawal events)
    tx_value: int = 0                              # native coin sent with the transaction
    other_events: list = field(default_factory=list)  # (emitter, topic0, count, first topic1, first data words)
    evidence: str = "events"                       # events: venues proven by Swap / fill events or the registry | guess: venues guessed from flows
    flags: list = field(default_factory=list)      # things worth a human look (an unexplained venue balance, a mismatch)
    liquidity: list = field(default_factory=list)  # decoded liquidity moves of singleton pools (ModifyLiquidity)
    venue_checks: list = field(default_factory=list)  # (holder, event total, net total, ok): events vs token movements


@dataclass
class Parsed:
    nets: dict
    transfers: list
    pools: set
    seen: set
    swaps: list
    lp: dict
    lp_events: list
    native_in: int
    native_out: int
    other: dict = field(default_factory=dict)   # (emitting address, topic0) -> [count, first topic1]


def parse_logs(receipt: dict, wrapped: str) -> Parsed:
    nets: dict[str, dict[str, int]] = {}
    transfers: list = []
    pools: set[str] = set()
    seen: set[str] = set()
    swaps: list = []
    lp: dict[str, set[str]] = {}
    lp_events: list = []
    other: dict = {}
    n_in = n_out = 0

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
        words = [int(data[i:i + 64], 16) for i in range(0, len(data) - 63, 64)]
        t0 = topics[0]
        if t0 == TRANSFER_TOPIC0 and len(topics) == 3 and len(data) >= 64:      # ERC-20 (ERC-721 has 4 topics)
            frm, to, amount = _addr(topics[1]), _addr(topics[2]), words[0]
            transfers.append((idx, token, frm, to, amount))
            add(frm, token, -amount)
            add(to, token, amount)
            seen.update((frm, to))
        elif t0 == DEPOSIT_TOPIC0 and token == wrapped and len(topics) == 2 and words:
            dst = _addr(topics[1]); add(dst, token, words[0]); seen.add(dst); n_in += words[0]
        elif t0 == WITHDRAWAL_TOPIC0 and token == wrapped and len(topics) == 2 and words:
            src = _addr(topics[1]); add(src, token, -words[0]); seen.add(src); n_out += words[0]
        elif t0 in SWAP_TOPICS:
            pools.add(token)
            if t0 == V2_SWAP_TOPIC and len(words) >= 4:
                swaps.append((token, idx, words[0] - words[2], words[1] - words[3]))   # in - out, pool's side
            elif len(words) >= 2:
                swaps.append((token, idx, _s256(words[0]), _s256(words[1])))           # signed, pool's side
            else:
                swaps.append((token, idx, 0, 0))
        elif t0 in MINT_TOPICS:
            lp.setdefault(token, set()).add("mint")
            a = (words[0], words[1]) if t0 == V2_MINT_TOPIC and len(words) >= 2 else (
                (words[2], words[3]) if len(words) >= 4 else (None, None))
            lp_events.append((token, idx, "mint", *a))
        elif t0 in BURN_TOPICS:
            lp.setdefault(token, set()).add("burn")
            a = (words[0], words[1]) if t0 == V2_BURN_TOPIC and len(words) >= 2 else (
                (words[1], words[2]) if len(words) >= 3 else (None, None))
            lp_events.append((token, idx, "burn", *a))
        elif t0 == COLLECT_TOPIC:
            lp.setdefault(token, set()).add("collect")
            a = (words[1], words[2]) if len(words) >= 3 else (None, None)
            lp_events.append((token, idx, "collect", *a))
        else:                                                    # events we do not decode: kept so they can be identified
            e = other.setdefault((token, t0), [0, topics[1] if len(topics) > 1 else "", words[:10]])
            e[0] += 1
    for a in list(nets):                         # drop zero nets: those addresses are relays
        nets[a] = {t: v for t, v in nets[a].items() if v != 0}
        if not nets[a]:
            del nets[a]
    return Parsed(nets, transfers, pools, seen, swaps, lp, lp_events, n_in, n_out, other)


def reconcile(receipt: dict, rize: str, wrapped: str = "", tx_from: str | None = None, tx_value: int = 0,
              venue_addrs=None, lp_extra=None, known_pools=None) -> Recon:
    """venue_addrs: addresses KNOWN to be venues in this transaction although they emit no classic Swap event
    (a V4 PoolManager / Infinity Vault that swapped, an RFQ contract that filled); lp_extra: address -> {"mint",
    "burn", "collect"} for liquidity moves of singleton pools. Both come from the registry decoders (venues.py).
    known_pools: every pool address of the registry. When a transaction already swaps, a known pool whose RIZE balance
    moved is part of the route (a venue), never the trader."""
    rize = rize.lower()
    wrapped = (wrapped or "").lower()
    P = parse_logs(receipt, wrapped)
    nets, lp = P.nets, P.lp
    pools = set(P.pools) | {a.lower() for a in (venue_addrs or ())}
    if pools:
        pools |= {a.lower() for a in (known_pools or ()) if nets.get(a.lower(), {}).get(rize)}
    for a, kinds in (lp_extra or {}).items():
        lp.setdefault(a.lower(), set()).update(kinds)
    r = Recon(verdict="unknown", nets=nets, transfers=sorted(P.transfers), swap_pools=sorted(pools),
              swap_count=len(P.swaps), swaps=sorted(P.swaps, key=lambda x: x[1]), lp_pools=lp,
              lp_events=sorted(P.lp_events, key=lambda x: x[1]), native_in=P.native_in, native_out=P.native_out,
              tx_value=tx_value,
              other_events=sorted((k[0], k[1], v[0], v[1], v[2]) for k, v in P.other.items()))
    rz = {a: n[rize] for a, n in nets.items() if n.get(rize)}              # parties with a net RIZE change
    others = lambda a: {t: v for t, v in nets.get(a, {}).items() if t != rize}  # noqa: E731
    moved = {a for a in P.seen if a != ZERO}
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
        r.venues = venues
        r.traders = sorted(a for a in rz if a not in pools)
        if sum_v == 0:
            r.verdict, r.note = "arbitrage", "pools exchanged RIZE with each other; net RIZE zero"
        else:
            r.verdict = "sell" if sum_v > 0 else "buy"
    else:
        # no recognised Swap event: liquidity add/remove, or a swap through a venue we cannot see
        r.evidence = "guess"
        frm = (tx_from or "").lower()
        same = [a for a in rz if a != ZERO and others(a) and all(_sign(v) == _sign(rz[a]) for v in others(a).values())]
        opp = [a for a in rz if a != ZERO and any(_sign(v) == -_sign(rz[a]) for v in others(a).values())]

        def pick(group_neg: list, group_pos: list) -> tuple[list, str]:
            """Which side is the trader / provider? The sender's side, else the smaller side, else the paying side."""
            if not group_neg or not group_pos:                   # only one side has an opposite-sign counterpart
                return (group_neg or group_pos), "only side with a counter-asset"
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
    if r.verdict not in ("sell", "buy", "arbitrage"):
        for a in r.traders:
            for t, v in others(a).items():
                r.trader_counter[t] = r.trader_counter.get(t, 0) + v
        return r

    # ---- a trade: pools, fills, then the role of every other address -------------------------------------
    direction = _sign(sum(rz[v] for v in r.venues))                 # +1 sold into pools, -1 bought, 0 round trip
    if direction:                                                   # a pool whose Swap event we did not recognise
        for a in sorted(rz):
            if (a not in r.venues and a != ZERO and _sign(rz[a]) == direction
                    and any(_sign(v) == -direction for v in others(a).values())):
                r.venues = sorted(r.venues + [a])
                r.note = (r.note + "; " if r.note else "") + f"venue without a recognised Swap event: {a[:10]}"
    venues = set(r.venues)
    r.pool_rize = sum(rz[v] for v in r.venues)
    for v in r.venues:
        counter = {t: abs(x) for t, x in others(v).items() if _sign(x) == -_sign(rz[v])}
        r.fills.append({"pool": v, "rize": abs(rz[v]), "counter": counter, "dir": _sign(rz[v])})   # +1: pool received RIZE
        for t, x in counter.items():
            r.gross_counter[t] = r.gross_counter.get(t, 0) + x

    if direction > 0:                                               # sell: who gave RIZE vs who skimmed some
        r.traders = sorted(a for a in rz if a not in venues and a != ZERO and rz[a] < 0)
        r.rize_fees = sorted((a, rz[a]) for a in rz if a not in venues and a != ZERO and rz[a] > 0)
    elif direction < 0:                                             # buy: the big receivers are buyers, small ones fees
        pos = {a: x for a, x in rz.items() if a not in venues and a != ZERO and x > 0}
        tot = sum(pos.values())
        r.traders = sorted(a for a, x in pos.items() if x * 10 >= tot) + sorted(
            a for a in rz if a not in venues and a != ZERO and rz[a] < 0)
        r.rize_fees = sorted((a, x) for a, x in pos.items() if x * 10 < tot)
    else:
        r.traders = sorted(a for a in rz if a not in venues and a != ZERO)
    r.trader_rize = sum(rz[a] for a in r.traders)

    for a in r.traders:
        for t, v in others(a).items():
            r.trader_counter[t] = r.trader_counter.get(t, 0) + v
    want = 1 if direction >= 0 else -1                              # sell: traders RECEIVE the counter; buy: they PAY it
    seen_by_trader = {t: x for t, x in r.trader_counter.items() if _sign(x) == want}
    r.counter_source = "trader" if seen_by_trader else "pools"
    r.trader_counter = seen_by_trader

    known = venues | set(r.traders) | {a for a, _ in r.rize_fees} | {ZERO}
    for a in sorted(nets):
        if a in known:
            continue
        o = others(a)
        pos = {t: v for t, v in o.items() if v > 0}
        neg = {t: v for t, v in o.items() if v < 0}
        if pos and neg:                                             # swapped the proceeds again (second hop)
            r.hops.append(a)
            continue
        for t, x in pos.items():
            base = r.gross_counter.get(t)
            share = Decimal(x) / Decimal(base) if base else None
            if direction == 0:
                r.payees.append((a, t, x, share))                   # the one who walks away with the gain
            elif share is not None and share >= Decimal("0.5"):
                r.payees.append((a, t, x, share))
            else:
                r.fees.append((a, t, x))
        if direction < 0:
            for t, x in neg.items():
                base = r.gross_counter.get(t)
                share = Decimal(-x) / Decimal(base) if base else None
                if share is not None and share >= Decimal("0.5"):
                    r.payers.append((a, t, -x, share))
    if direction == 0:                                              # gain of the round trip = what the pools gave up
        tokens = {t for v in venues for t in others(v)}
        for t in tokens:
            g = -sum(nets.get(v, {}).get(t, 0) for v in venues)
            if g:
                r.profit[t] = g
    return r


# ----------------------------------------------------------------------------------------------- checks
def swap_check(r: Recon, pool_tokens: dict, short=lambda a: a) -> list[str]:
    """Does the sum of a pool's Swap events equal what the pool really gained / lost in tokens?
    pool_tokens: pool -> (token0, token1). This is what allows reading trades from pool logs without receipts."""
    out = []
    by_pool: dict[str, list] = {}
    for pool, _, n0, n1 in r.swaps:
        by_pool.setdefault(pool, []).append((n0, n1))
    for pool, evs in sorted(by_pool.items()):
        toks = pool_tokens.get(pool)
        if not toks:
            out.append(f"   {short(pool)}: {len(evs)} swap event(s); token0/token1 unknown, not checked")
            continue
        e0, e1 = sum(a for a, _ in evs), sum(b for _, b in evs)
        t0, t1 = r.nets.get(pool, {}).get(toks[0], 0), r.nets.get(pool, {}).get(toks[1], 0)
        if pool in r.lp_pools:
            out.append(f"   {short(pool)}: swap events AND liquidity events in the same tx; not compared")
        else:
            ok = (e0 == t0 and e1 == t1)
            out.append(f"   {short(pool)}: {len(evs)} swap event(s) -> {'MATCH' if ok else 'DIFFERENT'}"
                       + ("" if ok else f" (events {e0}/{e1} vs transfers {t0}/{t1})"))
    return out


# ----------------------------------------------------------------------------------------------- report
def fmt_amount(raw: int, decimals: int = 18, places: int = 6) -> str:
    d = Decimal(raw) / (Decimal(10) ** decimals)
    s = f"{d:,.{places}f}".rstrip("0").rstrip(".") if places else f"{d:,.0f}"
    return s if s not in ("", "-0") else "0"


def report(r: Recon, rize: str, meta: dict, short=lambda a: a, stables=None, brief: bool = False,
           pool_tokens: dict | None = None) -> list[str]:
    """Readable lines. meta: token address -> (symbol, decimals). stables: addresses priced as one dollar."""
    rize = rize.lower()
    pool_tokens = pool_tokens or {}

    def sym(t: str) -> str:
        return meta.get(t, (t[:8] + "…", 18))[0]

    def dec(t: str) -> int:
        return meta.get(t, ("", 18))[1]

    def amt(t: str, raw: int) -> str:
        return fmt_amount(raw, dec(t)) + " " + sym(t)

    def is_stable(t: str) -> bool:
        return (t in stables) if stables is not None else sym(t).upper() in STABLE_SYMBOLS

    def usd_per_rize(counter_raw: int, token: str, rize_raw: int):
        if not rize_raw:
            return None
        return Decimal(counter_raw) / Decimal(10) ** dec(token) / (Decimal(rize_raw) / Decimal(10) ** 18)

    def role_of(a: str) -> str:
        return ("trader" if a in r.traders else "pool" if a in r.venues else "RIZE fee" if a in {x for x, _ in r.rize_fees}
                else "route swap" if a in r.hops else "pool (Swap event)" if a in r.swap_pools
                else "proceeds" if a in {x[0] for x in r.payees} else "fee" if a in {x[0] for x in r.fees} else "other")

    out = [f"VERDICT: {r.verdict.upper()}" + (f"   ({r.note})" if r.note else "")]
    out.append("Net change per address (zero nets hidden):")
    for a, n in sorted(r.nets.items(), key=lambda kv: -abs(kv[1].get(rize, 0))):
        parts = ", ".join(("+" if v > 0 else "-") + amt(t, abs(v)) for t, v in sorted(n.items(), key=lambda kv: kv[0] != rize))
        out.append(f"   {short(a)}  [{role_of(a)}]  {parts}")
    if r.relays:
        out.append("Relays (moved tokens, net zero, hidden): " + ", ".join(short(a) for a in r.relays))

    if r.verdict in ("sell", "buy"):
        sold = r.verdict == "sell"
        pool_r = abs(r.pool_rize)
        gross = " + ".join(amt(t, x) for t, x in r.gross_counter.items())
        out.append(f"{'SOLD' if sold else 'BOUGHT'} {amt(rize, pool_r)} {'into' if sold else 'out of'} {len(r.fills)} pool(s)"
                   + (f" for {gross}" if gross else "") + "  (pool side, exact)")
        who = "Seller" if sold else "Buyer"
        for a in r.traders:
            out.append(f"   {who} {short(a)} {'gave' if r.nets[a][rize] < 0 else 'received'} {amt(rize, abs(r.nets[a][rize]))}")
        total_t = abs(r.trader_rize)
        for a, x in r.rize_fees:
            pct = f" = {Decimal(abs(x)) * 100 / Decimal(max(total_t, 1)):.3f}% of the RIZE the {who.lower()} handled"
            out.append(f"   RIZE fee / skim: {short(a)} gets {amt(rize, abs(x))}{pct}")
        if r.trader_counter:
            got = " + ".join(amt(t, abs(x)) for t, x in r.trader_counter.items())
            out.append(f"   {who} {'received' if sold else 'paid'} {got} (their own net)")
            st = [t for t in r.trader_counter if is_stable(t)]
            if st:
                px = usd_per_rize(abs(r.trader_counter[st[0]]), st[0], abs(r.trader_rize))
                if px:
                    out.append(f"   Average price (what the {who.lower()} got/paid): ${px:.6f} per RIZE")
        for a, t, x, share in r.payees:
            out.append(f"   Proceeds delivered to {short(a)}: {amt(t, x)} = {share:.1%} of the pool amount "
                       f"(not to the {who.lower()}; could be their own other account or a settlement contract)")
        for a, t, x, share in r.payers:
            out.append(f"   Paid by {short(a)} (not the buyer): {amt(t, x)} = {share:.1%} of the pool amount")
        for a in r.hops:
            n = r.nets[a]
            out.append(f"   Other swap in the route at {short(a)}: " + " / ".join(("+" if v > 0 else "-") + amt(t, abs(v)) for t, v in n.items()))
        if sold and r.native_out:
            out.append(f"   Wrapped native unwrapped: {fmt_amount(r.native_out, 18)} ETH-equivalent paid out as native coin "
                       "(the recipient is not visible in the logs)")
        if not sold and r.tx_value:
            out.append(f"   Paid with native coin: transaction value {fmt_amount(r.tx_value, 18)} ETH")
        if not sold and r.tx_value and r.native_in and r.tx_value > r.native_in:
            extra = r.tx_value - r.native_in
            out.append(f"   Native coin not swapped: {fmt_amount(extra, 18)} ETH = {Decimal(extra) * 100 / Decimal(r.tx_value):.3f}% "
                       "of the transaction value (a fee taken in native coin, or change returned)")
        if not sold and r.native_in and not r.tx_value:
            out.append(f"   Native coin wrapped in this transaction: {fmt_amount(r.native_in, 18)}")
        for a, t, x in r.fees:
            base = r.gross_counter.get(t)
            pct = f"  = {Decimal(x) * 100 / Decimal(base):.3f}% of the pool amount" if base else ""
            out.append(f"   Fee / other recipient: {short(a)} receives {amt(t, x)}{pct}")
    elif r.verdict == "arbitrage" and r.fills:
        bought = [f for f in r.fills if f["dir"] < 0]
        sold = [f for f in r.fills if f["dir"] > 0]
        if bought and sold:
            def side(fs):
                tot: dict = {}
                for f in fs:
                    for t, x in f["counter"].items():
                        tot[t] = tot.get(t, 0) + x
                return " + ".join(amt(t, x) for t, x in tot.items())
            vol = min(sum(f["rize"] for f in bought), sum(f["rize"] for f in sold))
            out.append(f"ROUND TRIP of {amt(rize, vol)} between pools: bought at {', '.join(short(f['pool']) for f in bought)} "
                       f"for {side(bought)}, sold at {', '.join(short(f['pool']) for f in sold)} for {side(sold)}")
        for t, g in r.profit.items():
            out.append(f"   Gross gain of the round trip: {'+' if g > 0 else '-'}{amt(t, abs(g))} (before gas)")
        for a, t, x, _ in r.payees:
            out.append(f"   Gain goes to {short(a)}: {amt(t, x)}")
    elif r.verdict in ("liquidity_add", "liquidity_remove", "liquidity_rebalance", "fee_collect"):
        what = {"liquidity_add": "added", "liquidity_remove": "removed", "liquidity_rebalance": "moved (removed + added)",
                "fee_collect": "collected (fees / tokens)"}[r.verdict]
        out.append(f"Liquidity {what} by: " + ", ".join(short(a) for a in r.traders) + "   in pool(s): " +
                   ", ".join(f"{short(a)} [{'+'.join(sorted(r.lp_pools.get(a, [])))}]" for a in r.venues))
    elif r.verdict == "burn":
        out.append(f"RIZE destroyed (sent to the zero address): {amt(rize, abs(r.trader_rize))} by "
                   + ", ".join(short(a) for a in r.traders) + "   (typical of a cross-chain bridge send)")
    elif r.verdict == "mint":
        out.append(f"RIZE created (from the zero address): {amt(rize, abs(r.trader_rize))} for "
                   + ", ".join(short(a) for a in r.traders) + "   (typical of a cross-chain bridge delivery)")
    elif r.verdict == "transfer":
        out.append("From: " + ", ".join(short(a) for a in r.traders) + "   To: " + ", ".join(short(a) for a in r.venues))

    if r.fills:
        out.append(f"Fills ({len(r.fills)}):")
        for i, f in enumerate(r.fills, 1):
            c = " + ".join(amt(t, x) for t, x in f["counter"].items())
            out.append(f"   #{i} pool {short(f['pool'])}: {amt(rize, f['rize'])} {'in' if f['dir'] > 0 else 'out'} <-> {c}")
        on_v = sum(1 for sw in r.swaps if sw[0] in set(r.venues))
        out.append(f"Pool-side flow ({on_v} swap event(s) on these pools" + (
            f", {r.swap_count - on_v} on other pools" if r.swap_count > on_v else "") + "):")
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
            st = [t for t in cnt if is_stable(t)]
            if st:
                px = usd_per_rize(cnt[st[0]], st[0], tot)
                if px:
                    line += f"  -> ${px:.8f} per RIZE"
            out.append(line)
    if r.lp_events:
        out.append("Liquidity events decoded from the pools:")
        for pool, idx, kind, a0, a1 in r.lp_events:
            t = pool_tokens.get(pool)
            if t and a0 is not None:
                out.append(f"   log {idx:>3} {kind:<7} {short(pool)}: {amt(t[0], a0)} / {amt(t[1], a1)}")
            else:
                out.append(f"   log {idx:>3} {kind:<7} {short(pool)}: amounts {a0} / {a1} (token order unknown)")
    if r.swaps and pool_tokens:
        out.append("Swap events vs token movements (can trades be read from pool logs alone?):")
        out += swap_check(r, pool_tokens, short)
    if r.other_events and (not brief or not r.swap_pools):
        out.append("Other events in the receipt (not decoded; emitter, topic0 x count, first indexed value, data words):")

        def w(v: int) -> str:
            sv = v - (1 << 256) if v >= (1 << 255) else v
            return str(sv) if abs(sv) < 10 ** 30 else "0x…" + f"{v:x}"[-8:]
        for emitter, t0, n, t1, words in r.other_events[:12]:
            out.append(f"   {short(emitter)}  {t0}  x{n}  {t1[:18] + '…' if t1 else ''}  data[{', '.join(w(x) for x in words)}]")
        if len(r.other_events) > 12:
            out.append(f"   … and {len(r.other_events) - 12} more")
    if not brief:
        out.append(f"Raw transfers in the receipt ({len(r.transfers)}), in log order:")
        for idx, t, f, to, x in r.transfers:
            out.append(f"   log {idx:>3}: {short(f)} -> {short(to)}  {amt(t, x)}")
    return out
