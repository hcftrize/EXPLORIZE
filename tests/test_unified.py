"""The unified update: one transaction, published when everything is at the same blocks; anomalies are reported, never blocking."""
import datetime as dt
import os

import psycopg2
import pytest

from indexer import config, db, enrich as enrich_mod, unified
from indexer.registry import Registry
from tests.test_governance import FakeGovClient, governance

E18 = 10 ** 18
UTC = dt.timezone.utc
ZERO = bytes(20)
X, Y = (0x77).to_bytes(20, "big"), (0x88).to_bytes(20, "big")
LAST = 100_000


class Node(FakeGovClient):
    """A node for one chain: balances, governance state and a totalSupply that follow `self.balances`."""

    def __init__(self, supply=None):
        super().__init__()
        self.balances = {}
        self.forced_supply = supply

    def eth_call(self, to, data, block="latest"):
        if data == "0x18160ddd":
            return hex(self.forced_supply if self.forced_supply is not None else sum(self.balances.values()))
        return super().eth_call(to, data, block)

    def get_block_timestamps(self, blocks):
        return {b: 1_800_000_000 + b for b in blocks}


@pytest.fixture(autouse=True)
def _close_staged(monkeypatch):
    """A failing test must not leave a transaction open: the next test would wait for its locks."""
    opened = []
    real = db.connect_staged

    def tracked(u):
        c = real(u)
        opened.append(c)
        return c
    monkeypatch.setattr(db, "connect_staged", tracked)
    yield
    for c in opened:
        try:
            c.abort()
            c.close()
        except Exception:
            pass


def url():
    return os.environ["TEST_DATABASE_URL"]


def world(conn, *, base_extra=0):
    """Four chains, each with a mint of 1000 RIZE to X, all synced and enriched up to LAST; nodes that agree with the database."""
    chains = config.load_chains()
    nodes = {}
    with conn.cursor() as cur:
        for key, c in chains.items():
            cur.execute("INSERT INTO chain_state (chain_id, slug, token, start_block, last_block) VALUES (%s,%s,%s,1,%s) "
                        "ON CONFLICT (chain_id) DO UPDATE SET last_block = EXCLUDED.last_block, balances_block = NULL",
                        (c.chain_id, key, psycopg2.Binary(c.token_bytes), LAST))
            cur.execute("INSERT INTO enrich_state (chain_id, last_block) VALUES (%s,%s) "
                        "ON CONFLICT (chain_id) DO UPDATE SET last_block = EXCLUDED.last_block", (c.chain_id, LAST))
            cur.execute("INSERT INTO transfers VALUES (%s, 5, now(), %s, 0, %s, %s, %s)",
                        (c.chain_id, (c.chain_id).to_bytes(32, "big"), ZERO, X, 1000 * E18))
        conn.commit()
    for key in chains:
        n = Node()
        n.balances = {int.from_bytes(X, "big"): 1000 * E18}
        nodes[key] = n
    nodes["base"].balances[int.from_bytes(X, "big")] += base_extra
    return chains, nodes


def stub_phases(monkeypatch, ccip_stats=None):
    monkeypatch.setattr(unified, "sync_chain", lambda c, cl, conn, **k: {"head": LAST, "rows": 0, "stopped_early": False, "skipped": False})
    monkeypatch.setattr(enrich_mod, "enrich_chain", lambda *a, **k: {})
    monkeypatch.setattr(enrich_mod, "repair_traders", lambda *a, **k: {})
    monkeypatch.setattr(enrich_mod, "snapshot", lambda *a, **k: 0)
    stats = ccip_stats or {"burns_checked": 0, "mints_checked": 0, "messages": 0, "no_message": 0, "refreshed": 0, "deferred": 0,
                           "dropped": 0, "purged": 0}
    monkeypatch.setattr(unified, "ccip_sync", lambda *a, **k: dict(stats))


def other(sql):
    """What the site would see: another connection, outside the run's transaction."""
    c = psycopg2.connect(url())
    try:
        with c.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()
    finally:
        c.close()


def test_staged_connection_publishes_only_at_the_end(conn):
    s = db.connect_staged(url())
    with s.cursor() as cur:
        cur.execute("INSERT INTO unified_runs (started_at, ok) VALUES (now(), true)")
    s.commit()                                                    # a step "commits": still invisible
    assert other("SELECT count(*) FROM unified_runs") == [(0,)]
    with s.cursor() as cur:
        cur.execute("INSERT INTO unified_runs (started_at, ok) VALUES (now(), false)")
    s.rollback()                                                  # an error handled by the code: back to the last step
    with s.cursor() as cur:
        cur.execute("SELECT count(*) FROM unified_runs")
        assert cur.fetchone() == (1,)
    s.publish()
    assert other("SELECT count(*) FROM unified_runs") == [(1,)]
    s.close()


def test_staged_connection_abort_leaves_nothing(conn):
    s = db.connect_staged(url())
    with s.cursor() as cur:
        cur.execute("INSERT INTO unified_runs (started_at, ok) VALUES (now(), true)")
    s.commit()
    s.abort()
    assert other("SELECT count(*) FROM unified_runs") == [(0,)]
    s.close()


def test_a_consistent_run_is_published_whole(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    s = db.connect_staged(url())
    out = unified.run_unified(s, list(chains.values()), nodes, Registry.from_dir(), log=lambda m: None)
    assert out["holders"]["wallets"] >= 1 and out["balances"]["base"]["seeded"] is True
    assert other("SELECT count(*) FROM holders") == [(0,)]        # nothing visible before the end
    assert other("SELECT count(*) FROM unified_runs") == [(0,)]
    s.publish()
    s.close()
    assert other("SELECT count(*) FROM unified_runs WHERE ok") == [(1,)]
    assert other("SELECT count(*) FROM holders")[0][0] >= 1
    assert other("SELECT count(*) FROM holder_balances WHERE chain_id = 8453") == [(1,)]
    assert other("SELECT balances_block FROM chain_state WHERE chain_id = 8453") == [(LAST,)]
    assert out["anomalies"] == 0


def test_a_second_run_reads_only_what_changed(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    s = db.connect_staged(url())
    unified.run_unified(s, list(chains.values()), nodes, Registry.from_dir(), log=lambda m: None)
    s.publish()
    s.close()
    # a new transfer on Base: X sends 10 to Y, and the node moves on
    with conn.cursor() as cur:
        cur.execute("UPDATE chain_state SET last_block = %s WHERE chain_id = 8453", (LAST + 200,))
        cur.execute("UPDATE enrich_state SET last_block = %s WHERE chain_id = 8453", (LAST + 200,))
        cur.execute("INSERT INTO transfers VALUES (8453, %s, now(), %s, 0, %s, %s, %s)", (LAST + 100, b"\x09" * 32, X, Y, 10 * E18))
    conn.commit()
    nodes["base"].balances[int.from_bytes(X, "big")] -= 10 * E18
    nodes["base"].balances[int.from_bytes(Y, "big")] = 10 * E18
    monkeypatch.setattr(unified, "sync_chain", lambda c, cl, conn, **k: {"head": LAST + 200 if c.key == "base" else LAST, "rows": 1, "stopped_early": False})
    s = db.connect_staged(url())
    out = unified.run_unified(s, list(chains.values()), nodes, Registry.from_dir(), log=lambda m: None)
    s.publish()
    s.close()
    assert out["balances"]["base"]["seeded"] is False and out["balances"]["base"]["read"] <= 4     # X, Y, zero (+ margin rows)
    assert other("SELECT balance FROM holder_balances WHERE chain_id = 8453 AND addr = '\\x%s'" % Y.hex()) == [(10 * E18,)]
    assert other("SELECT count(*) FROM unified_runs WHERE ok") == [(2,)]


def run(conn_, chains, nodes, **kw):
    found = []
    s = db.connect_staged(url())
    try:
        out = unified.run_unified(s, list(chains.values()), nodes, Registry.from_dir(), log=lambda m: None, anomalies=found, **kw)
    except Exception:
        s.abort(); s.close()
        raise
    return s, out, found


def test_balances_that_do_not_add_up_to_the_supply_are_an_anomaly_not_a_stop(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    nodes["bsc"].forced_supply = 1000 * E18 + 1                  # one RIZE-wei exists that no transfer of ours explains
    s, out, found = run(conn, chains, nodes)
    s.publish(); s.close()
    kinds = {k for _, k, _ in found}
    assert {"supply / bsc", "balances / bsc"} <= kinds and not any("base" in k for k in kinds)
    assert other("SELECT count(*) FROM holder_balances WHERE chain_id = 56") == [(1,)]        # the data is published anyway
    assert other("SELECT count(*) FROM unified_runs WHERE ok") == [(1,)]
    assert out["anomalies"] == len(found)


def test_a_bridge_message_that_does_not_reconcile_is_an_anomaly_not_a_stop(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    now = dt.datetime.now(UTC)
    burn_tx, mint_tx = b"\x0b" * 32, b"\x0c" * 32
    nodes["base"].balances[int.from_bytes(X, "big")] -= 5 * E18      # the node agrees with the burn and with the (short) mint
    nodes["bsc"].balances[int.from_bytes(X, "big")] += 4 * E18
    with conn.cursor() as cur:
        cur.execute("INSERT INTO transfers VALUES (8453, 50, %s, %s, 0, %s, %s, %s)", (now, burn_tx, X, ZERO, 5 * E18))
        cur.execute("INSERT INTO transfers VALUES (56, 50, %s, %s, 0, %s, %s, %s)", (now, mint_tx, ZERO, X, 4 * E18))   # minted less
        cur.execute("INSERT INTO ccip_messages (message_id, status, source_chain_id, dest_chain_id, source_tx, dest_tx, amount, sent_at, received_at) "
                    "VALUES (%s,'SUCCESS',8453,56,%s,%s,%s,%s,%s)", (b"\x0d" * 32, burn_tx, mint_tx, 5 * E18, now, now))
    conn.commit()
    s, out, found = run(conn, chains, nodes)
    s.publish(); s.close()
    ccip = [f for f in found if f[1].startswith("ccip")]
    assert len(ccip) == 1 and ccip[0][0].startswith("ccip:") and "0x0d0d" in ccip[0][0]
    assert other("SELECT count(*) FROM unified_runs WHERE ok") == [(1,)]


def test_a_message_ahead_of_our_sync_is_not_an_anomaly(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    now = dt.datetime.now(UTC)
    burn_tx = b"\x1b" * 32
    nodes["base"].balances[int.from_bytes(X, "big")] -= 5 * E18
    with conn.cursor() as cur:
        cur.execute("INSERT INTO transfers VALUES (8453, 50, %s, %s, 0, %s, %s, %s)", (now, burn_tx, X, ZERO, 5 * E18))
        # received on BSC after the block we synced to (its timestamp is far in the future in this test): not in our transfers yet
        cur.execute("INSERT INTO ccip_messages (message_id, status, source_chain_id, dest_chain_id, source_tx, dest_tx, amount, sent_at, received_at) "
                    "VALUES (%s,'SUCCESS',8453,56,%s,%s,%s,%s,%s)",
                    (b"\x1d" * 32, burn_tx, b"\x1c" * 32, 5 * E18, now, now + dt.timedelta(days=4000)))
    conn.commit()
    s, out, found = run(conn, chains, nodes)
    assert out["ccip"]["ahead_of_sync"] == 1 and not [f for f in found if f[1].startswith("ccip")]
    s.abort(); s.close()


def test_a_burn_not_looked_up_in_ccip_yet_stops_the_run(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    old = dt.datetime.now(UTC) - dt.timedelta(hours=2)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO transfers VALUES (8453, 50, %s, %s, 0, %s, %s, %s)", (old, b"\x2b" * 32, X, ZERO, 5 * E18))
    conn.commit()
    with pytest.raises(unified.RunFailed) as e:
        run(conn, chains, nodes)
    assert "CCIP" in str(e.value) and "ccip:backlog" in e.value.codes
    assert other("SELECT count(*) FROM unified_runs") == [(0,)]


def test_trades_that_are_behind_the_transfers_stop_the_run(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    with conn.cursor() as cur:
        cur.execute("UPDATE enrich_state SET last_block = %s WHERE chain_id = 56", (LAST - 50,))
    conn.commit()
    with pytest.raises(unified.RunFailed) as e:
        run(conn, chains, nodes)
    assert "enrich:bsc" in e.value.codes
    assert other("SELECT count(*) FROM holders") == [(0,)]


def test_a_crashed_transaction_is_an_anomaly_not_a_stop(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO enrich_review (chain_id, tx_hash, block_number, reason, detail) VALUES (8453, %s, 99000, 'crash', 'boom')",
                    (b"\x43" * 32,))
    conn.commit()
    s, out, found = run(conn, chains, nodes)
    s.publish(); s.close()
    assert [f[0] for f in found if f[1].startswith("crash")] == ["crash:base:0x" + "43" * 32]


def test_balances_are_read_at_exactly_the_synced_block_and_a_reorged_address_is_re_read(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    with conn.cursor() as cur:                                   # X sent 10 to Y one block before the head (inside the reorg window)
        cur.execute("INSERT INTO transfers VALUES (8453, %s, now(), %s, 0, %s, %s, %s)", (LAST - 1, b"\x09" * 32, X, Y, 10 * E18))
    conn.commit()
    nodes["base"].balances[int.from_bytes(X, "big")] -= 10 * E18
    nodes["base"].balances[int.from_bytes(Y, "big")] = 10 * E18
    s, out, found = run(conn, chains, nodes)
    s.publish(); s.close()
    assert not found
    assert other("SELECT balances_block FROM chain_state WHERE chain_id = 8453") == [(LAST,)]
    assert other("SELECT count(*) FROM holder_balances WHERE chain_id = 8453") == [(2,)]

    def reorg(c, cl, conn_, **k):                                # the node reorganised: the transfer is gone, Y has nothing again
        if c.key == "base":
            with conn_.cursor() as cur:
                cur.execute("DELETE FROM transfers WHERE chain_id = 8453 AND tx_hash = %s", (b"\x09" * 32,))
            conn_.commit()
        return {"head": LAST, "rows": 0, "stopped_early": False}
    monkeypatch.setattr(unified, "sync_chain", reorg)
    nodes["base"].balances[int.from_bytes(X, "big")] = 1000 * E18
    nodes["base"].balances.pop(int.from_bytes(Y, "big"))
    s, out, found = run(conn, chains, nodes)
    s.publish(); s.close()
    assert not found                                             # without the pre-sync set Y would keep its stale 10 and the sum would break
    assert other("SELECT count(*) FROM holder_balances WHERE chain_id = 8453") == [(1,)]


def test_a_second_run_at_the_same_time_is_skipped(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    first = db.connect_staged(url())
    with first.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (unified.ADVISORY_KEY,))
    s = db.connect_staged(url())
    out = unified.run_unified(s, list(chains.values()), nodes, Registry.from_dir(), log=lambda m: None)
    assert out.get("skipped")
    s.abort(); s.close(); first.abort(); first.close()


def test_a_chain_far_from_its_head_after_the_budget_stops_the_run(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    monkeypatch.setattr(unified, "sync_chain", lambda c, cl, conn, **k: {"head": LAST + 5000, "rows": 0, "stopped_early": True})
    s = db.connect_staged(url())
    with pytest.raises(unified.RunFailed) as e:
        unified.run_unified(s, list(chains.values()), nodes, Registry.from_dir(), log=lambda m: None)
    assert "catch up" in str(e.value)
    s.abort(); s.close()


def test_the_top_holders_table_is_rebuilt_only_when_it_is_old_enough(conn, monkeypatch):
    chains, nodes = world(conn)
    stub_phases(monkeypatch)
    s, out, found = run(conn, chains, nodes)                       # first run: balances never read -> table built
    s.publish(); s.close()
    assert out["holders"]["rebuilt"] is True
    first = other("SELECT updated_at, base_block FROM holders_meta")
    s, out, found = run(conn, chains, nodes)                       # right after: balances stay current, table untouched
    s.publish(); s.close()
    assert out["holders"]["rebuilt"] is False and out["balances"]["base"]["seeded"] is False
    assert other("SELECT updated_at, base_block FROM holders_meta") == first
    assert other("SELECT count(*) FROM unified_runs WHERE ok") == [(2,)]
    with conn.cursor() as cur:                                     # a day later: rebuilt again
        cur.execute("UPDATE holders_meta SET updated_at = now() - interval '25 hours'")
    conn.commit()
    s, out, found = run(conn, chains, nodes)
    s.publish(); s.close()
    assert out["holders"]["rebuilt"] is True
    s, out, found = run(conn, chains, nodes, ranking_every_hours=0)  # 0 = every run
    s.publish(); s.close()
    assert out["holders"]["rebuilt"] is True
