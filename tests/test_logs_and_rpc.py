import pytest
import requests

from indexer.logs import TRANSFER_TOPIC0, decode_transfer, fetch_logs_adaptive
from indexer.rpc import RangeTooLarge, RpcClient, RpcError
from tests.fakes import FakeClient, make_log


def test_decode_transfer_ok():
    d = decode_transfer(make_log(10, 3, 0xAA, 0xBB, 12345, with_ts=1_700_000_000))
    assert d["block_number"] == 10 and d["log_index"] == 3
    assert d["amount"] == 12345
    assert d["from"] == (0xAA).to_bytes(20, "big") and d["to"] == (0xBB).to_bytes(20, "big")
    assert d["block_ts"] == 1_700_000_000


def test_decode_skips_nft_and_removed():
    nft = make_log(1, 0, 1, 2, 0)
    nft["topics"].append("0x" + "00" * 32)
    assert decode_transfer(nft) is None
    removed = make_log(1, 0, 1, 2, 5)
    removed["removed"] = True
    assert decode_transfer(removed) is None


def _logs(n_blocks, per_block=3):
    return [make_log(b, i, 1, 2, 1) for b in range(1, n_blocks + 1) for i in range(per_block)]


def test_adaptive_splits_on_range_limit_and_keeps_order():
    logs = _logs(1000)
    c = FakeClient(logs, 1000, max_range=64)
    stats = {}
    got = fetch_logs_adaptive(c, "0xtoken", [TRANSFER_TOPIC0], 1, 1000, 10_000, stats)
    assert len(got) == len(logs) and stats["splits"] > 0
    keys = [(int(l["blockNumber"], 16), int(l["logIndex"], 16)) for l in got]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)


def test_adaptive_detects_silent_truncation_at_cap():
    logs = _logs(500)  # 1500 logs; node silently returns only 100 per call
    c = FakeClient(logs, 500, cap=100, silent_truncate=True)
    got = fetch_logs_adaptive(c, "0xtoken", [TRANSFER_TOPIC0], 1, 500, 100, {})
    assert len(got) == len(logs)


def test_single_block_over_limit_raises():
    c = FakeClient(_logs(3), 3, max_range=0.5)
    with pytest.raises(RpcError):
        fetch_logs_adaptive(c, "0xtoken", [TRANSFER_TOPIC0], 1, 1, 10_000, {})


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


def _client_with(responses, monkeypatch):
    c = RpcClient("https://lb.drpc.live/base/SECRETKEY", max_rps=0, max_retries=3)
    it = iter(responses)

    def fake_post(url, json=None, timeout=None):
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(c._session, "post", fake_post)
    monkeypatch.setattr("indexer.rpc.time.sleep", lambda *_: None)
    return c


def test_range_error_becomes_rangetoolarge(monkeypatch):
    err = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32005, "message": "query returned more than 10000 results"}}
    c = _client_with([_Resp(200, err)], monkeypatch)
    with pytest.raises(RangeTooLarge):
        c.get_logs("0x1", [], 1, 100)


def test_retries_on_429_then_succeeds(monkeypatch):
    ok = {"jsonrpc": "2.0", "id": 1, "result": "0x10"}
    c = _client_with([_Resp(429, None), _Resp(503, None), _Resp(200, ok)], monkeypatch)
    assert c.block_number() == 16


def test_timeout_on_getlogs_is_range_error(monkeypatch):
    c = _client_with([requests.Timeout(), requests.Timeout()], monkeypatch)
    with pytest.raises(RangeTooLarge):
        c.get_logs("0x1", [], 1, 100)


def test_network_error_never_leaks_url(monkeypatch):
    boom = requests.ConnectionError("HTTPSConnectionPool(host='lb.drpc.live'): /base/SECRETKEY failed")
    c = _client_with([boom] * 10, monkeypatch)
    with pytest.raises(RpcError) as e:
        c.block_number()
    assert "SECRETKEY" not in str(e.value)


def test_batch_returns_results_in_order(monkeypatch):
    body = [{"jsonrpc": "2.0", "id": 1, "result": {"timestamp": "0x20"}},
            {"jsonrpc": "2.0", "id": 0, "result": {"timestamp": "0x10"}}]  # out of order on purpose
    c = _client_with([_Resp(200, body)], monkeypatch)
    assert c.get_block_timestamps([5, 6]) == {5: 16, 6: 32}
