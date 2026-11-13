"""The governance diagnostic and the Multicall3 helpers, against a fake chain state."""
from indexer import config, governance, multicall as mc
from indexer.rpc import RpcError

W = 32


def word(n: int) -> bytes:
    return int(n).to_bytes(W, "big")


def pad(b: bytes) -> bytes:
    return b + b"\x00" * (-len(b) % W)


def parse_aggregate3(hex_data: str):
    """Independent parser of an aggregate3 request: returns [(target, calldata)]."""
    b = bytes.fromhex(hex_data[2 + 8:])
    arr = int.from_bytes(b[:W], "big")
    n = int.from_bytes(b[arr:arr + W], "big")
    base = arr + W
    out = []
    for i in range(n):
        t = base + int.from_bytes(b[base + W * i: base + W * i + W], "big")
        target = "0x" + b[t + 12:t + 32].hex()
        allow = int.from_bytes(b[t + W:t + 2 * W], "big")
        assert allow == 1
        boff = int.from_bytes(b[t + 2 * W:t + 3 * W], "big")
        ln = int.from_bytes(b[t + boff:t + boff + W], "big")
        out.append((target.lower(), b[t + boff + W:t + boff + W + ln]))
    return out


def encode_results(results):
    """results: [(ok, bytes)] -> aggregate3 answer."""
    tuples = [word(1 if ok else 0) + word(2 * W) + word(len(d)) + pad(d) for ok, d in results]
    head, off = b"", W * len(tuples)
    for t in tuples:
        head += word(off)
        off += len(t)
    return "0x" + (word(W) + word(len(tuples)) + head + b"".join(tuples)).hex()


def release_bytes(entries):
    return word(W) + word(len(entries)) + b"".join(word(a) + word(t) for a, t in entries)


class FakeGovClient:
    """Bonds 1..8 exist (id 4 burned); bond 3 has a release queue; bond 5 is vested."""

    def __init__(self, *, balance_extra=0, multicall_code="0x6080", fail_big_groups=False):
        self.owner = {1: 0xA1, 2: 0xA1, 3: 0xB2, 5: 0xC3, 6: 0xA1, 7: 0xD4, 8: 0xD4}
        self.bonded = {1: 100, 2: 50, 3: 10, 5: 70, 6: 5, 7: 1, 8: 2}
        self.queue = {3: [(30, 2_000_000_000), (20, 1_000)]}
        self.vested = {5: 40}
        e = 10 ** 18
        self.scale = e
        self.balance_extra = balance_extra
        self.code = multicall_code
        self.fail_big_groups = fail_big_groups
        self.group_sizes = []
        self.token = None

    def total(self):
        return (sum(self.bonded.values()) + sum(a for q in self.queue.values() for a, _ in q)) * self.scale

    # --- RpcClient surface used by the diagnostic
    def get_code(self, address, block="latest"):
        return self.code

    def chain_id(self):
        return 8453

    def block_number(self):
        return 1234

    def get_block_timestamps(self, blocks):
        return {b: 1_800_000_000 for b in blocks}

    def eth_call(self, to, data, block="latest"):
        assert to.lower() == mc.MULTICALL3.lower()
        calls = parse_aggregate3(data)
        self.group_sizes.append(len(calls))
        if self.fail_big_groups and len(calls) > 100:
            raise RpcError("out of gas")
        out = []
        for target, cd in calls:
            sel, arg = cd[:4].hex(), int.from_bytes(cd[4:36], "big") if len(cd) >= 36 else None
            s = self.scale
            if sel == mc.SEL_BALANCE_OF:
                out.append((True, word(self.total() + self.balance_extra)))
            elif sel == mc.SEL_TOTAL_SUPPLY:
                out.append((True, word(len(self.owner))))
            elif sel == mc.SEL_GET_RELEASE_WARMUP:
                out.append((True, word(31 * 86400)))
            elif sel == mc.SEL_OWNER_OF:
                out.append((True, word(self.owner[arg])) if arg in self.owner else (False, b""))
            elif sel == mc.SEL_BONDED:
                out.append((True, word(self.bonded.get(arg, 0) * s)))
            elif sel == mc.SEL_BROKEN_BOND:
                out.append((True, word(sum(a for a, _ in self.queue.get(arg, [])) * s)))
            elif sel == mc.SEL_VESTED:
                out.append((True, word(self.vested.get(arg, 0) * s)))
            elif sel == mc.SEL_GET_BOND_RELEASE:
                out.append((True, release_bytes([(a * s, t) for a, t in self.queue.get(arg, [])])))
            elif sel == mc.SEL_GET_SCHEDULE:
                out.append((True, word(100 * s) + word(0) + word(365 * 86400) + word(1_700_000_000)))
            elif sel == mc.SEL_GET_VESTING_INITIATOR:
                out.append((True, word(0xEE)))
            else:
                out.append((False, b""))
        return encode_results(out)


def chains():
    return config.load_chains()


class OnChain:
    """The same fake state, answering with the right chain id for each chain."""
    def __init__(self, fake, chain_id):
        self._fake, self._cid = fake, chain_id

    def chain_id(self):
        return self._cid

    def __getattr__(self, name):
        return getattr(self._fake, name)


def run(fake, conn=None, max_id=200):
    lines = []
    clients = {k: OnChain(fake, c.chain_id) for k, c in chains().items()}
    ok = governance.run(clients, chains(), max_id, conn, lines.append)
    return ok, "\n".join(lines)


def test_multicall_roundtrip_with_failures():
    class C:
        def eth_call(self, to, data, block="latest"):
            calls = parse_aggregate3(data)
            return encode_results([(i % 2 == 0, word(i)) for i, _ in enumerate(calls)])
    res = mc.multicall(C(), [("0x" + "11" * 20, b"\x01\x02\x03\x04")] * 5)
    assert [ok for ok, _ in res] == [True, False, True, False, True]
    assert mc.dec_uint(res[2][1]) == 2


def test_multicall_halves_a_refused_group():
    f = FakeGovClient(fail_big_groups=True)
    res = mc.multicall(f, [(governance.BOND_NFT, mc.call_uint(mc.SEL_OWNER_OF, 1))] * 500, group=400)
    assert len(res) == 500 and all(ok for ok, _ in res)
    assert max(f.group_sizes[-3:]) <= 100


def test_decoders():
    assert mc.dec_release_list(release_bytes([(5, 6), (7, 8)])) == [(5, 6), (7, 8)]
    assert mc.dec_release_list(release_bytes([])) == []
    assert mc.dec_release_list(b"") == []
    assert mc.dec_address(word(0xABCD)) == "0x" + "00" * 18 + "abcd"


def test_invariant_holds_and_report_is_sound():
    ok, out = run(FakeGovClient())
    assert ok
    assert "Multicall3 PRESENT" in out
    assert "bonds with an owner: 7" in out
    assert "highest existing id: 8" in out
    assert "ids WITHOUT owner but with RIZE bonded/queued: 0" in out
    assert "-> OK (exact)" in out
    assert "bonds whose queue does not add up to brokenBond: 0" in out
    assert "bonds with a pending release: 1   bonds still vesting: 1" in out
    assert "bond #5" in out and "initiator 0x" in out        # vesting detail
    assert "distinct bond owners: 4" in out


def test_invariant_break_is_reported():
    ok, out = run(FakeGovClient(balance_extra=5_000 * 10 ** 18))
    assert "DIFFERENT" in out


def test_missing_multicall_fails_the_run():
    ok, out = run(FakeGovClient(multicall_code="0x"))
    assert not ok and "MISSING" in out


def test_scan_limit_too_small_is_flagged():
    ok, out = run(FakeGovClient(), max_id=10)   # highest id 8 is within 50 of the limit
    assert not ok and "scan limit is too close" in out


def test_database_cross_check(conn):
    gov = bytes.fromhex(governance.GOVERNANCE[2:].lower())
    f = FakeGovClient()
    owner_a = bytes.fromhex(("%040x" % 0xA1))
    other = b"\x77" * 20
    with conn.cursor() as cur:
        # the contract received 160 RIZE in total from owner A; owner A is the only owner seen in our transfers
        cur.execute("INSERT INTO transfers VALUES (8453, 10, now(), %s, 0, %s, %s, %s)",
                    (b"\x01" * 32, owner_a, gov, 160 * 10 ** 18))
        cur.execute("INSERT INTO transfers VALUES (8453, 11, now(), %s, 0, %s, %s, %s)",
                    (b"\x02" * 32, gov, other, 10 * 10 ** 18))
    conn.commit()
    ok, out = run(f, conn)
    assert "contract balance rebuilt from our transfers: 150.0000" in out
    assert "bond owners that never appear in any of our transfers: 3 of 4" in out
