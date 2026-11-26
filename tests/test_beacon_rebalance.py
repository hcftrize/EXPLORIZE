"""Proxy Beacon (0x376d39cf...), the liquidity manager of the Aero 1 position, against its REAL transactions of 2026-11.

Each transaction is one rebalance: collect the old position (Burn + Collect), swap a little on the two USDC/RIZE pools to
get the ratio right (Aero 1 and Aerodrome 1), then mint the new position (Mint). The pool of the position has both swap and
liquidity events, or the swap sits on the other pool.

Before the fix the net RIZE of the pools mixed the swap with the Mint and the Collect, so the stored trade had a wrong side,
a wrong amount ("<0.001 RIZE") or a wrong price ($0.0148, $0.087), and the liquidity line counted the same tokens twice
(Burn + the Collect that hands the Burn's tokens out). The data below is read from the receipts, to the wei: transfers of RIZE
and USDC, and the Swap / Mint / Burn / Collect events of the pools (T idx R|U from to amount; S/M/B/C idx pool amount0 amount1).
Middle addresses are shortened to their first 8 hex digits, and the other events of the receipts are left out.
"""
from decimal import Decimal

import pytest

from indexer import enrich
from indexer.logs import TRANSFER_TOPIC0
from indexer.receipts import COLLECT_TOPIC, reconcile
from indexer.registry import Registry

REG = Registry.from_dir()
RIZE = "0x9818b6c09f5ecc843060927e8587c427c7c93583"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
WETH = "0x4200000000000000000000000000000000000006"
AERO1 = "0x929312aff9e4611bba5da6adcd08b31eb487bbb5"        # position of the beacon, USDC/RIZE
AERODROME1 = "0xea5cb64754ad7aa24f7a6bbe3b724f29b4f822b8"   # the other USDC/RIZE pool
BEACON = "0x376d39cf45fd24c2275aef40b0a9416bba75ed24"
KEEPER = "0xed19aa23" + "00" * 16
KNOWN = {"929312af": AERO1, "ea5cb647": AERODROME1, "376d39cf": BEACON, "ed19aa23": KEEPER}
SWAP_V3 = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
MINT_V3 = "0x7a53080ba414158be7ec69b987b5fb7d07dee101fe85488f0853ae16239d0bde"
BURN_V3 = "0x0c396cd989a39f4459b5fa1aed6a9a8dcdbc45908acfd67e028cd568da98982c"

DATA = """
## 1ff77f2f
B,150,929312af,0,0
C,151,929312af,0,0
B,170,929312af,53576015,45396055838140254449584
T,174,U,929312af,376d39cf,53576015
T,175,R,929312af,376d39cf,45396055838140254449584
C,176,929312af,53576015,45396055838140254449584
T,180,U,376d39cf,4f4ee076,2673651
T,182,R,ea5cb647,d374c006,1085838496451998989619
T,183,U,4f4ee076,ea5cb647,2524652
S,184,ea5cb647,2524652,-1085838496451998989619
T,186,R,929312af,d374c006,64073991199727678375
T,187,U,4f4ee076,929312af,148999
S,188,929312af,148999,-64073991199727678375
T,190,R,d374c006,4f4ee076,1149912487651726667994
T,191,R,4f4ee076,376d39cf,1149912487651726667994
T,197,U,376d39cf,929312af,56930799
T,198,R,376d39cf,929312af,48161365626244814251032
M,199,929312af,56930799,48161365626244814251032
B,203,929312af,0,0
C,204,929312af,0,0
## 74d75d40
B,71,929312af,0,0
C,72,929312af,0,0
B,91,929312af,42498643,41487692123641225658998
T,95,U,929312af,376d39cf,42498643
T,96,R,929312af,376d39cf,41487692123641225658998
C,97,929312af,42498643,41487692123641225658998
T,101,U,376d39cf,4f4ee076,12843060
T,103,U,4f4ee076,4fe93ebc,12843060
T,105,U,4fe93ebc,bedfac74,12843060
T,107,U,bedfac74,939e398d,12843060
T,109,R,929312af,939e398d,5523761014951861897839
T,110,U,939e398d,929312af,12843060
S,111,929312af,12843060,-5523761014951861897839
T,112,R,939e398d,bedfac74,5523761014951861897839
T,114,R,bedfac74,4f4ee076,5523761014951861897839
T,116,R,4f4ee076,376d39cf,5523761014951861897839
T,122,U,376d39cf,929312af,53576016
T,123,R,376d39cf,929312af,45396055838140254449585
M,124,929312af,53576016,45396055838140254449585
B,128,929312af,0,0
C,129,929312af,0,0
## d867bf19
B,58,929312af,0,0
C,59,929312af,0,0
B,78,929312af,160346118,0
T,82,U,929312af,376d39cf,160346118
C,83,929312af,160346118,0
T,87,U,376d39cf,4f4ee076,96296062
T,89,U,4f4ee076,4fe93ebc,96296062
T,91,U,4fe93ebc,bedfac74,96296062
T,93,U,bedfac74,939e398d,96296062
T,95,R,929312af,939e398d,23135650583324438323305
T,96,U,939e398d,929312af,53748586
S,97,929312af,53748586,-23135650583324438323305
T,98,R,ea5cb647,939e398d,18352041540316787364745
T,99,U,939e398d,ea5cb647,42547476
S,100,ea5cb647,42547476,-18352041540316787364745
T,101,R,939e398d,bedfac74,41487692123641225688050
T,103,R,bedfac74,4f4ee076,41487692123641225688050
T,105,R,4f4ee076,376d39cf,41487692123641225688050
T,111,U,376d39cf,929312af,42498644
T,112,R,376d39cf,929312af,41487692123641225658999
M,113,929312af,42498644,41487692123641225658999
B,117,929312af,0,0
C,118,929312af,0,0
## 58578beb
B,41,929312af,0,0
C,42,929312af,0,0
B,61,929312af,94703381,25150533110946998110736
T,65,U,929312af,376d39cf,94703381
T,66,R,929312af,376d39cf,25150533110946998110736
C,67,929312af,94703381,25150533110946998110736
T,71,U,376d39cf,4f4ee076,3050064
T,73,U,4f4ee076,4fe93ebc,3050064
T,75,U,4fe93ebc,bedfac74,3050064
T,77,U,bedfac74,939e398d,3050064
T,78,R,ea5cb647,939e398d,1328829305009261777124
T,79,U,939e398d,ea5cb647,3050064
S,80,ea5cb647,3050064,-1328829305009261777124
T,81,R,939e398d,bedfac74,1328829305009261777124
T,83,R,bedfac74,4f4ee076,1328829305009261777124
T,85,R,4f4ee076,376d39cf,1328829305009261777124
T,91,U,376d39cf,929312af,99707038
T,92,R,376d39cf,929312af,26479362415956259897099
M,93,929312af,99707038,26479362415956259897099
B,97,929312af,0,0
C,98,929312af,0,0
## 4ea72527
B,53,929312af,0,0
C,54,929312af,0,0
B,73,929312af,162373143,0
T,77,U,929312af,376d39cf,162373143
C,78,929312af,162373143,0
T,82,U,376d39cf,4f4ee076,57247019
T,84,U,4f4ee076,4fe93ebc,57247019
T,86,U,4fe93ebc,bedfac74,57247019
T,88,U,bedfac74,939e398d,57247019
T,90,R,929312af,939e398d,20674762476143632505597
T,91,U,939e398d,929312af,47705412
S,92,929312af,47705412,-20674762476143632505597
T,93,R,ea5cb647,939e398d,4135195772403746276312
T,94,U,939e398d,ea5cb647,9541607
S,95,ea5cb647,9541607,-4135195772403746276312
T,96,R,939e398d,bedfac74,24809958248547378781909
T,98,R,bedfac74,4f4ee076,24809958248547378781909
T,100,R,4f4ee076,376d39cf,24809958248547378781909
T,106,U,376d39cf,929312af,94703382
T,107,R,376d39cf,929312af,25150533110946998110737
M,108,929312af,94703382,25150533110946998110737
B,112,929312af,0,0
C,113,929312af,0,0
## 26f401fe
B,36,929312af,0,0
C,37,929312af,0,0
B,56,929312af,103169703,24889261268908649184104
T,60,U,929312af,376d39cf,103169703
T,61,R,929312af,376d39cf,24889261268908649184104
C,62,929312af,103169703,24889261268908649184104
T,66,R,376d39cf,4f4ee076,768407806228581005403
T,68,U,ea5cb647,d374c006,1746196
T,69,R,4f4ee076,ea5cb647,768407806228581005403
S,70,ea5cb647,-1746196,768407806228581005403
T,72,U,d374c006,4f4ee076,1746196
T,73,U,4f4ee076,376d39cf,1746196
T,79,U,376d39cf,929312af,104957607
T,80,R,376d39cf,929312af,25320585492894950572799
M,81,929312af,104957607,25320585492894950572799
B,85,929312af,0,0
C,86,929312af,0,0
## 81dce8e8
B,78,929312af,0,0
C,79,929312af,0,0
B,98,929312af,30022650,42961648470986828490817
T,102,U,929312af,376d39cf,30022650
T,103,R,929312af,376d39cf,42961648470986828490817
C,104,929312af,30022650,42961648470986828490817
T,108,U,376d39cf,4f4ee076,21068546
T,111,R,929312af,d374c006,8094178971111615609157
T,112,U,4f4ee076,929312af,18434977
S,113,929312af,18434977,-8094178971111615609157
T,114,R,ea5cb647,d374c006,1156378833729207323586
T,115,U,4f4ee076,ea5cb647,2633569
S,116,ea5cb647,2633569,-1156378833729207323586
T,118,R,d374c006,4f4ee076,9250557804840822932743
T,119,R,4f4ee076,376d39cf,9250557804840822932743
T,125,U,376d39cf,929312af,44915962
T,126,R,376d39cf,929312af,50671899383213149756599
M,127,929312af,44915962,50671899383213149756599
B,131,929312af,0,0
C,132,929312af,0,0
## 42e2ed2a
T,56,U,376d39cf,4f4ee076,97482292
T,58,R,ea5cb647,d374c006,30236977873570182617098
T,59,U,4f4ee076,ea5cb647,68547493
S,60,ea5cb647,68547493,-30236977873570182617098
T,62,R,929312af,d374c006,12711730863023560773441
T,63,U,4f4ee076,929312af,28934799
S,64,929312af,28934799,-12711730863023560773441
T,66,R,d374c006,4f4ee076,42948708733479131672406
T,67,R,4f4ee076,376d39cf,42948708733479131672406
T,73,U,376d39cf,929312af,30022651
T,74,R,376d39cf,929312af,42961648470986828490818
M,75,929312af,30022651,42961648470986828490818
B,79,929312af,0,0
C,80,929312af,0,0
## ab5dc017
B,13,929312af,0,0
C,14,929312af,0,0
B,34,929312af,155268099,2477576191489896137789
T,38,U,929312af,376d39cf,155268099
T,39,R,929312af,376d39cf,2477576191489896137789
C,40,929312af,155268099,2477576191489896137789
T,44,R,376d39cf,4f4ee076,43171057662224538176
T,46,U,ea5cb647,d374c006,93581
T,47,R,4f4ee076,ea5cb647,43171057662224538176
S,48,ea5cb647,-93581,43171057662224538176
T,50,U,d374c006,4f4ee076,93581
T,51,U,4f4ee076,376d39cf,93581
T,57,U,376d39cf,929312af,155361680
T,58,R,376d39cf,929312af,2479069427331107781658
M,59,929312af,155361680,2479069427331107781658
B,63,929312af,0,0
C,64,929312af,0,0
## 1dbb6263
B,32,929312af,0,0
C,33,929312af,0,0
B,53,929312af,141567312,8622844537081271626427
T,57,U,929312af,376d39cf,141567312
T,58,R,929312af,376d39cf,8622844537081271626427
C,59,929312af,141567312,8622844537081271626427
T,63,R,376d39cf,4f4ee076,176960343103361658363
T,65,U,ea5cb647,d374c006,381947
T,66,R,4f4ee076,ea5cb647,176960343103361658363
S,67,ea5cb647,-381947,176960343103361658363
T,69,U,d374c006,4f4ee076,381947
T,70,U,4f4ee076,376d39cf,381947
T,76,U,376d39cf,929312af,141949259
T,77,R,376d39cf,929312af,8646108812638723523366
M,78,929312af,141949259,8646108812638723523366
B,82,929312af,0,0
C,83,929312af,0,0
## 98aa5ab9
B,61,929312af,0,0
C,62,929312af,0,0
B,81,929312af,139863045,8519037880874708683880
T,85,U,929312af,376d39cf,139863045
T,86,R,929312af,376d39cf,8519037880874708683880
C,87,929312af,139863045,8519037880874708683880
T,91,R,376d39cf,4f4ee076,789496018780759270086
T,93,U,ea5cb647,d374c006,1704268
T,94,R,4f4ee076,ea5cb647,789496018780759270086
S,95,ea5cb647,-1704268,789496018780759270086
T,97,U,d374c006,4f4ee076,1704268
T,98,U,4f4ee076,376d39cf,1704268
T,104,U,376d39cf,929312af,141567313
T,105,R,376d39cf,929312af,8622844537081271626428
M,106,929312af,141567313,8622844537081271626428
B,110,929312af,0,0
C,111,929312af,0,0
## d77203b9
B,148,929312af,0,0
C,149,929312af,0,0
B,168,929312af,132359860,8062019968922391405163
T,172,U,929312af,376d39cf,132359860
T,173,R,929312af,376d39cf,8062019968922391405163
C,174,929312af,132359860,8062019968922391405163
T,178,R,376d39cf,4f4ee076,3473408206475772967481
T,180,U,ea5cb647,d374c006,7502530
T,181,R,4f4ee076,ea5cb647,3473408206475772967481
S,182,ea5cb647,-7502530,3473408206475772967481
T,183,U,75ad3d90,d374c006,656
T,186,U,d374c006,4f4ee076,7503186
T,187,U,4f4ee076,376d39cf,7503186
T,193,U,376d39cf,929312af,139863046
T,194,R,376d39cf,929312af,8519037880874708683881
M,195,929312af,139863046,8519037880874708683881
B,199,929312af,0,0
C,200,929312af,0,0
"""

# what each transaction really is: side, RIZE swapped, USDC (6 decimals) paid or received, RIZE removed from / added to the position
EXPECTED = {
    "1ff77f2f": ("buy", 1149912487651726667994, 2673651, 45396055838140254449584, 48161365626244814251032),
    "74d75d40": ("buy", 5523761014951861897839, 12843060, 41487692123641225658998, 45396055838140254449585),
    "d867bf19": ("buy", 41487692123641225688050, 96296062, 0, 41487692123641225658999),
    "58578beb": ("buy", 1328829305009261777124, 3050064, 25150533110946998110736, 26479362415956259897099),
    "4ea72527": ("buy", 24809958248547378781909, 57247019, 0, 25150533110946998110737),
    "26f401fe": ("sell", 768407806228581005403, 1746196, 24889261268908649184104, 25320585492894950572799),
    "81dce8e8": ("buy", 9250557804840822932743, 21068546, 42961648470986828490817, 50671899383213149756599),
    "42e2ed2a": ("buy", 42948708736593743390539, 97482292, 0, 42961648470986828490818),
    "ab5dc017": ("sell", 43171057662224538176, 93581, 2477576191489896137789, 2479069427331107781658),
    "1dbb6263": ("sell", 176960343103361658363, 381947, 8622844537081271626427, 8646108812638723523366),
    "98aa5ab9": ("sell", 789496018780759270086, 1704268, 8519037880874708683880, 8622844537081271626428),
    "d77203b9": ("sell", 3473408206475772967481, 7502530, 8062019968922391405163, 8519037880874708683881),
}


def t32(a: str) -> str:
    return "0x" + "0" * 24 + a[2:]


def w(n: int) -> str:
    return f"{n & ((1 << 256) - 1):064x}"


def addr(short: str) -> str:
    return KNOWN.get(short) or "0x" + short + "00" * 16


def parse(text: str) -> dict[str, list[list[str]]]:
    out: dict[str, list[list[str]]] = {}
    key = None
    for line in text.strip().splitlines():
        if line.startswith("## "):
            key = line[3:].strip()
            out[key] = []
        else:
            out[key].append(line.split(","))
    return out


def receipt(rows: list[list[str]]) -> dict:
    logs = []

    def add(address, topics, data):
        logs.append({"address": address, "topics": topics, "data": "0x" + data, "logIndex": hex(len(logs))})

    for kind, _idx, *rest in rows:
        if kind == "T":
            token, frm, to, amount = rest
            add(RIZE if token == "R" else USDC, [TRANSFER_TOPIC0, t32(addr(frm)), t32(addr(to))], w(int(amount)))
        else:
            pool, a0, a1 = addr(rest[0]), int(rest[1]), int(rest[2])
            if kind == "S":
                add(pool, [SWAP_V3, t32(addr("d374c006")), t32(addr("d374c006"))], "".join(w(x) for x in (a0, a1, 2 ** 90, 10 ** 20, 337066)))
            elif kind == "M":
                add(pool, [MINT_V3, t32(BEACON), "0x" + "00" * 32, "0x" + "00" * 32], "".join(w(x) for x in (int(BEACON, 16), 10 ** 15, a0, a1)))
            elif kind == "B":
                add(pool, [BURN_V3, t32(BEACON), "0x" + "00" * 32, "0x" + "00" * 32], "".join(w(x) for x in (10 ** 15, a0, a1)))
            else:
                add(pool, [COLLECT_TOPIC, t32(BEACON), "0x" + "00" * 32, "0x" + "00" * 32], "".join(w(x) for x in (int(BEACON, 16), a0, a1)))
    return {"logs": logs, "status": "0x1", "blockNumber": hex(62_500_000), "from": KEEPER, "to": BEACON}


TXS = parse(DATA)


def built(key):
    return enrich.build("base", REG, receipt(TXS[key]), {"from": KEEPER, "to": BEACON, "value": "0x0"})


def test_the_dataset_has_the_twelve_transactions():
    assert set(TXS) == set(EXPECTED)


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_the_trade_is_the_swap_alone(key):
    side, rize, usdc, _removed, _added = EXPECTED[key]
    t = built(key).trade
    assert t is not None
    assert t["side"] == side and t["rize"] == rize                         # the swap, not swap + Mint - Collect
    assert t["counter"] == {USDC: str(usdc)}
    assert t["usd"] == Decimal(usdc) / Decimal(10) ** 6
    assert t["trader"] == BEACON and t["sender"] == KEEPER
    assert Decimal("0.0021") < t["price_usd"] < Decimal("0.0025")          # the real price, never $0.0148 or $0.087
    assert t["flags"] == []


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_the_liquidity_rows_do_not_count_the_burn_twice(key):
    _side, _rize, _usdc, removed, added = EXPECTED[key]
    rows = [x for x in built(key).liquidity if x["pool"] == AERO1]
    assert sum(-x["rize"] for x in rows if x["rize"] < 0) == removed       # Burn, then the Collect of the same tokens: counted once
    assert sum(x["rize"] for x in rows if x["rize"] > 0) == added
    assert {x["kind"] for x in rows} <= {"add", "remove"}                  # no collect row: it carried nothing but the Burn's tokens
    assert all(x["provider"] == BEACON for x in rows)


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_nothing_is_left_for_review(key):
    b = built(key)
    assert b.review is None and b.unknown_pools == []


def test_the_swap_on_the_position_pool_itself_leaves_the_position_flows_aside():
    """74d75d40: Aero 1 swaps AND collects AND mints. Its net RIZE was -1,615.397 (swap -5,523.761, mint +45,396.056,
    collect -41,487.692); the trade is the 5,523.761."""
    rec = receipt(TXS["74d75d40"])
    r = reconcile(rec, RIZE, WETH, tx_from=KEEPER, known_pools={AERO1, AERODROME1})
    assert r.verdict == "buy" and r.venues == [AERO1] and r.traders == [BEACON]
    assert r.nets[AERO1][RIZE] == -5523761014951861897839
    assert r.nets[BEACON][RIZE] == 5523761014951861897839
    assert sum(x[5] for x in r.lp_set_aside if x[2] == RIZE) == 41487692123641225658998 + 45396055838140254449585
    assert r.flags == []


def test_a_swap_on_the_other_pool_does_not_make_the_position_pool_a_venue():
    """58578beb: the swap is on Aerodrome 1; Aero 1 only collects and mints. Its RIZE balance moved (+1,328.829) and the old
    rule took it for a second venue of the trade (sum of venues ~ 0: a phantom SELL of '<0.001 RIZE' priced $3.05)."""
    rec = receipt(TXS["58578beb"])
    r = reconcile(rec, RIZE, WETH, tx_from=KEEPER, known_pools={AERO1, AERODROME1})
    assert r.verdict == "buy" and r.venues == [AERODROME1] and r.traders == [BEACON]
    assert AERO1 not in r.nets or not r.nets[AERO1].get(RIZE)


def test_a_collect_that_does_not_match_the_transfers_before_it_is_reported_not_guessed():
    rows = [row[:] for row in TXS["74d75d40"]]
    for row in rows:
        if row[0] == "C" and row[1] == "97":
            row[3] = "42498644"                                           # the event says one micro-USDC more than the pool paid
    r = reconcile(receipt(rows), RIZE, WETH, tx_from=KEEPER, known_pools={AERO1, AERODROME1})
    assert any(f.startswith("liquidity_flows_unmatched") for f in r.flags)


def test_without_any_swap_the_move_is_still_a_rebalance():
    rows = [row for row in TXS["74d75d40"] if row[0] != "S" and not (row[0] == "T" and row[1] in {"101", "103", "105", "107", "109", "110", "112", "114", "116"})]
    r = reconcile(receipt(rows), RIZE, WETH, tx_from=KEEPER, known_pools={AERO1, AERODROME1})
    assert r.verdict == "liquidity_rebalance" and r.lp_set_aside == []


def test_a_normal_collect_after_a_burn_with_fees_counts_only_the_fees_as_a_collection():
    rz, fees, usdc = 1000 * 10 ** 18, 7 * 10 ** 18, 5 * 10 ** 6
    rows = [["B", "1", "929312af", str(usdc), str(rz)],
            ["T", "2", "U", "929312af", "376d39cf", str(usdc)], ["T", "3", "R", "929312af", "376d39cf", str(rz + fees)],
            ["C", "4", "929312af", str(usdc), str(rz + fees)]]
    b = enrich.build("base", REG, receipt(rows), {"from": KEEPER, "to": BEACON, "value": "0x0"})
    by = {x["kind"]: x for x in b.liquidity}
    assert by["remove"]["rize"] == -rz and by["collect"]["rize"] == -fees and "counter" in by["collect"]
    assert sum(-x["rize"] for x in b.liquidity) == rz + fees               # what really left the pool
