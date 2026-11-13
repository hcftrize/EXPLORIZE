"""The Top Holders job: live balances on every chain + bonds read from the contract, one atomic snapshot."""
import pytest

from indexer import config, holders
from indexer.rpc import RpcError
from tests.test_governance import FakeGovClient, OnChain, governance

E18 = 10 ** 18
GOV = bytes.fromhex(governance.GOVERNANCE[2:].lower())


def a(n: int) -> bytes:
    return n.to_bytes(20, "big")


X, Y, Z = a(0x77), a(0x88), a(0x99)               # X: big free holder; Y: empty wallet; Z: contract with 0 live balance
A1, B2, C3, D4 = a(0xA1), a(0xB2), a(0xC3), a(0xD4)   # bond owners (C3 and D4 never appear in a transfer)


def add_transfers(conn):
    ids = {k: c.chain_id for k, c in config.load_chains().items()}
    rows = [
        (ids["base"], X, A1), (ids["base"], X, GOV), (ids["base"], A1, GOV),
        (ids["ethereum"], X, A1),
        (ids["bsc"], Z, Y),
    ]
    with conn.cursor() as cur:
        cur.execute("DELETE FROM transfers; DELETE FROM holders; DELETE FROM bonds; DELETE FROM holders_meta")
        for n, (cid, f, t) in enumerate(rows, 1):
            cur.execute("INSERT INTO transfers VALUES (%s, %s, now(), %s, 0, %s, %s, %s)",
                        (cid, n, n.to_bytes(32, "big"), f, t, 1 * E18))
    conn.commit()


def make_clients(*, fail_eth=False):
    base = FakeGovClient()
    base.balances = {int.from_bytes(X, "big"): 500 * E18, int.from_bytes(A1, "big"): 1 * E18}
    eth = FakeGovClient()
    eth.balances = {int.from_bytes(X, "big"): 1 * E18, int.from_bytes(A1, "big"): 4 * E18}
    eth.fail_balances = fail_eth
    bsc = FakeGovClient()
    bsc.balances = {}
    poly = FakeGovClient()
    poly.balances = {}
    ch = config.load_chains()
    fakes = {"base": base, "ethereum": eth, "bsc": bsc, "polygon": poly}
    return {k: OnChain(fakes[k], ch[k].chain_id) for k in ch}


def q(conn, sql, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def test_ranking_totals_and_atomic_snapshot(conn):
    add_transfers(conn)
    lines = []
    assert holders.run(make_clients(), config.load_chains(), conn, lines.append)
    rows = q(conn, "SELECT rank, address, held, bonded, queued, vested_locked, bond_count, total FROM holders ORDER BY rank")
    got = [(r[0], bytes(r[1]), int(r[2]) // E18, int(r[3]) // E18, int(r[4]) // E18, int(r[5]) // E18, r[6], int(r[7]) // E18)
           for r in rows]
    assert got == [
        (1, X, 501, 0, 0, 0, 0, 501),      # 500 on Base + 1 on Ethereum, no bond
        (2, A1, 5, 155, 0, 0, 3, 160),     # 1 + 4 held; three bonds
        (3, C3, 0, 70, 0, 40, 1, 70),      # never in a transfer, found through the bond owner; 40 still vesting
        (4, B2, 0, 10, 50, 0, 1, 60),      # 50 queued for release counts in the total
        (5, D4, 0, 3, 0, 0, 2, 3),
    ]
    assert not any(bytes(r[1]) in (GOV, Y, Z) for r in rows)       # contract, empty wallet, zero-balance contract
    assert q(conn, "SELECT count(*) FROM bonds")[0][0] == 7        # bond 4 does not exist
    ((ids_sum,),) = q(conn, "SELECT sum(nft_id) FROM bonds")
    assert ids_sum == 1 + 2 + 3 + 5 + 6 + 7 + 8
    (vest,) = q(conn, "SELECT vesting FROM bonds WHERE nft_id = 5")[0]
    assert vest["initiator"] == "0x" + "00" * 19 + "ee" and vest["vesting"] == 365 * 86400
    (queue,) = q(conn, "SELECT release_queue FROM bonds WHERE nft_id = 3")[0]
    assert [e["amount"] for e in queue] == [str(30 * E18), str(20 * E18)]
    meta = q(conn, "SELECT holders, bonds_active, invariant_diff, base_block FROM holders_meta")
    assert meta == [(5, 7, 0, 1234)]
    (by_chain,) = q(conn, "SELECT held_by_chain FROM holders WHERE rank = 1")[0]
    assert by_chain == {"base": str(500 * E18), "ethereum": str(1 * E18)}

    # running again replaces, never duplicates
    holders.run(make_clients(), config.load_chains(), conn, lines.append)
    assert q(conn, "SELECT count(*) FROM holders")[0][0] == 5
    assert q(conn, "SELECT count(*) FROM holders_meta")[0][0] == 1


def test_a_failed_read_keeps_the_previous_ranking(conn):
    add_transfers(conn)
    holders.run(make_clients(), config.load_chains(), conn, lambda m: None)
    before = q(conn, "SELECT rank, address, total FROM holders ORDER BY rank")
    with pytest.raises(RpcError):
        holders.run(make_clients(fail_eth=True), config.load_chains(), conn, lambda m: None)
    conn.rollback()
    assert q(conn, "SELECT rank, address, total FROM holders ORDER BY rank") == before


def test_invariant_difference_is_recorded(conn):
    add_transfers(conn)
    clients = make_clients()
    clients["base"]._fake.balance_extra = 7 * E18       # RIZE sent straight to the contract, outside any bond
    holders.run(clients, config.load_chains(), conn, lambda m: None)
    assert q(conn, "SELECT invariant_diff FROM holders_meta")[0][0] == 7 * E18


def test_dry_run_writes_nothing(conn):
    add_transfers(conn)
    holders.run(make_clients(), config.load_chains(), conn, lambda m: None, dry_run=True)
    assert q(conn, "SELECT count(*) FROM holders")[0][0] == 0


def test_top_is_cut_at_1000(conn, monkeypatch):
    add_transfers(conn)
    monkeypatch.setattr(holders, "TOP_N", 3)
    holders.run(make_clients(), config.load_chains(), conn, lambda m: None)
    assert [int(r[0]) for r in q(conn, "SELECT rank FROM holders ORDER BY rank")] == [1, 2, 3]
