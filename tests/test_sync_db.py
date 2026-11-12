import pytest

from indexer import db
from indexer.checks import verify_chain
from indexer.config import Chain
from indexer.sync import find_start_block, sync_chain
from tests.fakes import FakeClient, make_log

CHAIN = Chain(key="testchain", chain_id=999, name="Test", drpc_slug="x",
              token="0x" + "11" * 20, start_block=None, reorg_margin=20,
              initial_chunk=500, min_chunk=10, max_chunk=4000)


def build_chain(n=3000):
    logs = []
    for b in range(5, n + 1, 7):
        logs.append(make_log(b, 0, 0xA1, 0xB2, 1000 + b))
        logs.append(make_log(b, 1, 0xB2, 0xC3, 10))
    return logs


def count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM transfers WHERE chain_id = 999")
        n = cur.fetchone()[0]
    conn.commit()
    return n


def test_full_sync_is_idempotent(conn):
    logs = build_chain()
    c = FakeClient(logs, 3000, max_range=300, deploy_block=5)
    r1 = sync_chain(CHAIN, c, conn, log=lambda *_: None)
    assert count(conn) == len(logs) and r1["reached"] == 3000
    assert db.get_state(conn, 999) == (5, 3000)  # start block was auto-discovered
    sync_chain(CHAIN, c, conn, log=lambda *_: None)
    sync_chain(CHAIN, c, conn, log=lambda *_: None)
    assert count(conn) == len(logs)


def test_block_time_taken_from_header_fallback_and_from_logs(conn):
    logs = build_chain(100)
    c = FakeClient(logs, 100, deploy_block=5)
    sync_chain(CHAIN, c, conn, log=lambda *_: None)
    assert c.block_ts_requests > 0
    with conn.cursor() as cur:
        cur.execute("SELECT EXTRACT(EPOCH FROM block_time)::bigint FROM transfers WHERE block_number = 5 LIMIT 1")
        assert cur.fetchone()[0] == 1_700_000_000 + 5 * 2
    conn.commit()
    c2 = FakeClient(logs, 100, deploy_block=5, ts_in_logs=True)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM transfers; UPDATE chain_state SET last_block = NULL")
    conn.commit()
    sync_chain(CHAIN, c2, conn, log=lambda *_: None)
    assert c2.block_ts_requests == 0


def test_reorg_replaces_recent_blocks(conn):
    logs = build_chain(1000)
    c = FakeClient(logs, 1000, deploy_block=5)
    sync_chain(CHAIN, c, conn, log=lambda *_: None)
    # the chain reorgs inside the margin: one transfer disappears, another appears, head advances
    last_blocks = [l for l in logs if int(l["blockNumber"], 16) > 985]
    assert last_blocks
    gone = last_blocks[0]
    c.logs = [l for l in c.logs if l is not gone] + [make_log(990, 5, 0xD4, 0xE5, 777)]
    c.head = 1100
    sync_chain(CHAIN, c, conn, log=lambda *_: None)
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM transfers WHERE tx_hash = decode(%s, 'hex') AND log_index = %s",
                    (gone["transactionHash"][2:], int(gone["logIndex"], 16)))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT COUNT(*) FROM transfers WHERE amount = 777")
        assert cur.fetchone()[0] == 1
    conn.commit()
    assert count(conn) == len(c.logs)
    assert db.get_state(conn, 999)[1] == 1100


def test_stops_cleanly_and_resumes(conn):
    logs = build_chain(3000)
    c = FakeClient(logs, 3000, deploy_block=5)
    r = sync_chain(CHAIN, c, conn, max_seconds=0.0, log=lambda *_: None)  # budget already spent
    assert r["stopped_early"] and count(conn) == 0
    sync_chain(CHAIN, c, conn, log=lambda *_: None)
    assert count(conn) == len(logs)


def test_chunk_adapts_when_node_refuses_big_ranges(conn):
    logs = build_chain(3000)
    c = FakeClient(logs, 3000, max_range=100, deploy_block=5)
    sync_chain(CHAIN, c, conn, log=lambda *_: None)
    assert count(conn) == len(logs)


def test_supply_check_ok_and_mismatch(conn):
    logs = [make_log(10, 0, 0, 0xA1, 5000),      # mint 5000
            make_log(11, 0, 0xA1, 0xB2, 1200),   # normal transfer
            make_log(12, 0, 0xB2, 0, 200)]       # burn 200
    c = FakeClient(logs, 20, deploy_block=1, total_supply=4800)
    sync_chain(CHAIN, c, conn, log=lambda *_: None)
    assert verify_chain(CHAIN, c, conn, log=lambda *_: None) is True
    c.total_supply = 4801
    assert verify_chain(CHAIN, c, conn, log=lambda *_: None) is False


def test_find_start_block_binary_search():
    c = FakeClient([], 10_000_000, deploy_block=4_321_987)
    assert find_start_block(c, CHAIN, 10_000_000, log=lambda *_: None) == 4_321_987


def test_find_start_block_wrong_address():
    c = FakeClient([], 1000, deploy_block=5000)  # no code at head
    with pytest.raises(SystemExit):
        find_start_block(c, CHAIN, 1000, log=lambda *_: None)


def test_chunk_size_does_not_oscillate_when_node_has_a_ceiling(conn):
    big = Chain(key="testchain", chain_id=999, name="Test", drpc_slug="x",
                token="0x" + "11" * 20, start_block=None, reorg_margin=20,
                initial_chunk=20000, min_chunk=10, max_chunk=200000)
    logs = [make_log(b, 0, 0xA1, 0xB2, 1) for b in range(10, 2_000_000, 5000)]
    c = FakeClient(logs, 2_000_000, max_range=5000, deploy_block=10)
    sync_chain(big, c, conn, log=lambda *_: None)
    assert count(conn) == len(logs)
    assert c.get_logs_calls < 450  # without a learned ceiling it needs ~530


def test_sync_retries_a_chunk_after_a_transient_rpc_error(conn, monkeypatch):
    from indexer.rpc import RpcError

    class Flaky(FakeClient):
        def get_logs(self, address, topics, lo, hi):
            if self.get_logs_calls < 2:
                self.get_logs_calls += 1
                raise RpcError("pruned history unavailable")
            return super().get_logs(address, topics, lo, hi)

    monkeypatch.setattr("indexer.sync.time.sleep", lambda *_: None)
    logs = build_chain(2000)
    c = Flaky(logs, 2000, deploy_block=5)
    sync_chain(CHAIN, c, conn, log=lambda *_: None)
    assert count(conn) == len(logs)
