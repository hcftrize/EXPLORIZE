"""A fake RPC node over a synthetic chain, with configurable limits."""
from indexer.logs import TRANSFER_TOPIC0
from indexer.rpc import RangeTooLarge

ZERO = "0x" + "00" * 20


def addr_topic(n: int) -> str:
    return "0x" + n.to_bytes(32, "big").hex()


def make_log(block, idx, frm, to, amount, tx=None, with_ts=None):
    log = {
        "blockNumber": hex(block),
        "logIndex": hex(idx),
        "transactionHash": tx or "0x" + (block * 1000 + idx).to_bytes(32, "big").hex(),
        "topics": [TRANSFER_TOPIC0, addr_topic(frm), addr_topic(to)],
        "data": "0x" + amount.to_bytes(32, "big").hex(),
    }
    if with_ts is not None:
        log["blockTimestamp"] = hex(with_ts)
    return log


class FakeClient:
    def __init__(self, logs, head, *, max_range=None, cap=None, silent_truncate=False,
                 deploy_block=1, ts_in_logs=False, total_supply=None):
        self.logs = list(logs)
        self.head = head
        self.max_range = max_range
        self.cap = cap
        self.silent_truncate = silent_truncate
        self.deploy_block = deploy_block
        self.ts_in_logs = ts_in_logs
        self.total_supply = total_supply
        self.get_logs_calls = 0
        self.block_ts_requests = 0

    def block_number(self):
        return self.head

    def get_logs(self, address, topics, lo, hi):
        self.get_logs_calls += 1
        if self.max_range and hi - lo + 1 > self.max_range:
            raise RangeTooLarge("range too large")
        out = [dict(l) for l in self.logs if lo <= int(l["blockNumber"], 16) <= hi]
        if self.ts_in_logs:
            for l in out:
                l["blockTimestamp"] = hex(1_700_000_000 + int(l["blockNumber"], 16) * 2)
        if self.cap and len(out) > self.cap:
            if self.silent_truncate:
                return out[: self.cap]
            raise RangeTooLarge("too many logs")
        return out

    def get_block_timestamps(self, blocks):
        self.block_ts_requests += 1
        return {b: 1_700_000_000 + b * 2 for b in blocks}

    def get_code(self, address, block="latest"):
        n = self.head if block == "latest" else block
        return "0x6080" if n >= self.deploy_block else "0x"

    def eth_call(self, to, data, block="latest"):
        assert data == "0x18160ddd"
        return hex(self.total_supply)
