"""Adapters for the venues that are not "one pool = one contract that emits its own Swap event".

Three families, all verified on real RIZE transactions:

* Uniswap V4 (Ethereum) and PancakeSwap Infinity (BNB Chain) are singletons: ONE manager contract holds many pools, tells
  them apart by a 32-byte pool id (first indexed value of the event), and emits
      Swap(id, sender, amount0, amount1, sqrtPriceX96, liquidity, tick, fee [, protocolFee])      amounts: the SWAPPER's side,
                                                                                                  negative = the swapper pays
      ModifyLiquidity(id, sender, tickLower, tickUpper, liquidityDelta, salt)
  The tokens sit in the manager (Uniswap) or in a separate Vault (Pancake). Native ETH / BNB legs leave no token log: the
  amounts of the event are the only exact source for them.
* A DODO private pool (Ethereum, "DODO 1", kind rfq in the registry): a contract that holds RIZE and USDT and fills swaps itself, event
      Fill(tokenA, tokenB, amountA, amountB, ...)   with the two tokens in either order.

This module turns the logs of one receipt into (a) the venue addresses to hand to `receipts.reconcile`, and (b) after the
reconciliation, exact per-pool fills and liquidity moves. Pure functions, no network.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .receipts import ZERO, Recon, _addr, _s256
from .registry import NATIVE, SINGLETON_KINDS, Registry

UNISWAP_V4_SWAP = "0x40e9cecb9f5f1f1c5b9c97dec2917b7ee92e57ba5563708daca94dd84ad7112f"
INFINITY_SWAP = "0x04206ad2b7c0f463bff3dd4f33c5735b0f2957a351e4f79763a4fa9e775dd237"
SINGLETON_SWAP_TOPICS = {UNISWAP_V4_SWAP, INFINITY_SWAP}
MODIFY_LIQUIDITY = "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec"   # same signature on both
RFQ_FILL = "0xc2c0245e056d5fb095f04cd6373bc770802ebd1e6c918eb78fdef843cdb37b0f"


@dataclass
class SingletonSwap:
    pool: str            # pool id
    emitter: str
    log_index: int
    amount0: int         # swapper's side: negative = the swapper pays it
    amount1: int
    liquidity: int
    tick: int
    fee: int


@dataclass
class Modify:
    pool: str
    emitter: str
    log_index: int
    tick_lower: int
    tick_upper: int
    delta: int           # liquidity delta: > 0 add, < 0 remove, 0 = collect fees of the position
    salt: int


@dataclass
class RfqFill:
    emitter: str
    log_index: int
    token_a: str
    token_b: str
    amount_a: int
    amount_b: int


@dataclass
class VenueInputs:
    venue_addrs: set = field(default_factory=set)      # holders that swapped (managers / vaults / rfq contracts)
    lp_extra: dict = field(default_factory=dict)       # holder -> {"mint", "burn", "collect"}
    swaps: list = field(default_factory=list)          # SingletonSwap of REGISTERED pools
    modifies: list = field(default_factory=list)       # Modify of REGISTERED pools
    rfq: list = field(default_factory=list)            # RfqFill of registered RFQ contracts
    unknown_ids: dict = field(default_factory=dict)    # pool id -> emitter, for singleton pools that are not registered


def _words(data: str) -> list[int]:
    d = (data or "0x")[2:]
    return [int(d[i:i + 64], 16) for i in range(0, len(d) - 63, 64)]


def decode_venues(receipt: dict, chain: str, reg: Registry) -> VenueInputs:
    singles = reg.singleton_ids(chain)
    rfq_addrs = reg.rfq_addresses(chain)
    vi = VenueInputs()
    for lg in receipt.get("logs") or []:
        if lg.get("removed"):
            continue
        topics = [t.lower() for t in lg.get("topics") or []]
        if not topics:
            continue
        t0, emitter = topics[0], lg["address"].lower()
        idx = int(lg["logIndex"], 16) if isinstance(lg.get("logIndex"), str) else int(lg.get("logIndex") or 0)
        w = _words(lg.get("data"))
        if t0 in SINGLETON_SWAP_TOPICS and len(topics) >= 3 and len(w) >= 6:
            pid = topics[1]
            ev = SingletonSwap(pid, emitter, idx, _s256(w[0]), _s256(w[1]), w[3], _s256(w[4]), w[5])
            pool = singles.get(pid)
            if pool is None:
                vi.unknown_ids[pid] = emitter
            else:
                vi.swaps.append(ev)
                vi.venue_addrs.add(pool.holder_addr)
        elif t0 == MODIFY_LIQUIDITY and len(topics) >= 3 and len(w) >= 4:
            pid = topics[1]
            pool = singles.get(pid)
            if pool is not None:
                m = Modify(pid, emitter, idx, _s256(w[0]), _s256(w[1]), _s256(w[2]), w[3])
                vi.modifies.append(m)
                kind = "mint" if m.delta > 0 else "burn" if m.delta < 0 else "collect"
                vi.lp_extra.setdefault(pool.holder_addr, set()).add(kind)
        elif t0 == RFQ_FILL and emitter in rfq_addrs and len(w) >= 4:
            vi.rfq.append(RfqFill(emitter, idx, "0x" + f"{w[0]:040x}"[-40:], "0x" + f"{w[1]:040x}"[-40:], w[2], w[3]))
            vi.venue_addrs.add(emitter)
    return vi


def refine(r: Recon, vi: VenueInputs, chain: str, reg: Registry, rize: str) -> Recon:
    """After the reconciliation: replace the net-based fill of every singleton holder by one exact fill per pool id
    (the manager's balance mixes all its pools and omits native legs), attach liquidity moves, and cross-check the
    events against the token movements."""
    rize = rize.lower()
    singles = reg.singleton_ids(chain)
    by_holder: dict[str, dict[str, dict]] = {}
    for s in sorted(vi.swaps, key=lambda x: x.log_index):
        pool = singles[s.pool]
        a_r, a_c = (s.amount0, s.amount1) if pool.token0 == rize else (s.amount1, s.amount0)
        slot = by_holder.setdefault(pool.holder_addr, {}).setdefault(
            pool.key, {"pool": pool.key, "rr": 0, "rc": 0, "ctok": pool.counter_token(rize), "n": 0})
        slot["rr"] += a_r
        slot["rc"] += a_c
        slot["n"] += 1
    for holder, pools in by_holder.items():
        keep = [f for f in r.fills if f["pool"] != holder]
        new = []
        for slot in pools.values():
            if slot["rr"] == 0:
                continue
            new.append({"pool": slot["pool"], "rize": abs(slot["rr"]),
                        "counter": ({slot["ctok"]: abs(slot["rc"])} if slot["rc"] else {}),
                        "dir": 1 if slot["rr"] < 0 else -1,           # swapper paid RIZE -> the pool received it
                        "events": slot["n"]})
        r.fills = sorted(keep + new, key=lambda f: -f["rize"])
        event_total = sum(-s["rr"] for s in pools.values())            # what the holder gained in RIZE according to events
        net_total = r.nets.get(holder, {}).get(rize, 0)
        ok = event_total == net_total
        r.venue_checks.append((holder, event_total, net_total, ok))
        if not ok:
            r.flags.append(f"events_vs_transfers:{holder[:10]} events {event_total} vs net {net_total}")
    for f in vi.rfq:                                                   # an RFQ fill must equal the contract's own net
        a_r = f.amount_a if f.token_a == rize else f.amount_b if f.token_b == rize else None
        if a_r is None:
            continue
        net = r.nets.get(f.emitter, {}).get(rize, 0)
        if abs(net) != a_r:
            r.flags.append(f"rfq_event_vs_transfers:{f.emitter[:10]} event {a_r} vs net {net}")
        r.venue_checks.append((f.emitter, a_r if net >= 0 else -a_r, net, abs(net) == a_r))
    if by_holder or vi.rfq:                                            # counters changed: rebuild the gross amounts
        gross: dict = {}
        for f in r.fills:
            for t, x in f["counter"].items():
                gross[t] = gross.get(t, 0) + x
        r.gross_counter = gross
        r.pool_rize = sum(f["rize"] * f["dir"] for f in r.fills)
    # A holder that swaps AND moves liquidity in the same transaction (a liquidity manager rebalancing a V4 / Infinity position):
    # its net RIZE is swap + liquidity, so the side read from the nets can be the opposite of the swap. The swap events are exact
    # (the fills above come from them): the side is theirs.
    mixed = set(by_holder) & {singles[m.pool].holder_addr for m in vi.modifies}
    if mixed and r.fills:
        net = sum(f["rize"] * f["dir"] for f in r.fills)                # + the pools received RIZE: a sale
        r.verdict = "sell" if net > 0 else "buy" if net < 0 else "arbitrage"
        venues = {f["pool"] for f in r.fills} | set(r.venues)
        r.traders = sorted(a for a, n in r.nets.items() if a not in venues and a != ZERO and n.get(rize))
        r.note = (r.note + "; " if r.note else "") + "swap and liquidity move share the holder: the side is read from the swap events"
    for m in vi.modifies:
        r.liquidity.append({"pool": m.pool, "log": m.log_index, "tick_lower": m.tick_lower, "tick_upper": m.tick_upper,
                            "delta": m.delta, "salt": m.salt,
                            "kind": "add" if m.delta > 0 else "remove" if m.delta < 0 else "collect"})
    # amounts of a liquidity move: only when one pool id moved and no swap shares the holder (otherwise the
    # holder's balance mixes things and we say nothing rather than a wrong number)
    touched = {singles[m.pool].holder_addr for m in vi.modifies}
    for holder in touched:
        ids = {m.pool for m in vi.modifies if singles[m.pool].holder_addr == holder}
        if len(ids) == 1 and holder not in by_holder:
            pool = singles[next(iter(ids))]
            n = r.nets.get(holder, {})
            ct = pool.counter_token(rize)
            for lq in r.liquidity:
                if lq["pool"] == pool.key:
                    lq["rize"] = n.get(rize, 0)                       # + RIZE entered the pool, - left it
                    lq["counter"] = {ct: n.get(ct, 0)} if ct != NATIVE and n.get(ct, 0) else {}
                    break
    # singleton pools that are not in the registry but moved the holder's RIZE: say so, never guess
    known_holders = {p.holder_addr for p in singles.values()}
    for pid, emitter in vi.unknown_ids.items():
        if emitter in {p.manager for p in singles.values()} or emitter in known_holders:
            r.flags.append(f"unknown_pool_id:{pid[:18]}")
    return r


def holder_tokens(reg: Registry, chain: str) -> set[str]:
    """Every address whose RIZE balance is a venue balance (used to pick the transactions to enrich)."""
    return set(reg.holders(chain)) - {ZERO}
