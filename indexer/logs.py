"""Decode ERC-20 Transfer logs and fetch ranges with automatic splitting."""

from __future__ import annotations

from .rpc import RangeTooLarge, RpcError

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC0 = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO_ADDRESS = bytes(20)


def decode_transfer(log: dict) -> dict | None:
    """Return a flat dict for an ERC-20 Transfer log, or None if it is not one
    (ERC-721 transfers have 4 topics; removed logs are skipped)."""
    topics = log.get("topics") or []
    if log.get("removed") or len(topics) != 3 or topics[0].lower() != TRANSFER_TOPIC0:
        return None
    data = log.get("data") or "0x"
    ts = log.get("blockTimestamp")
    return {
        "block_number": int(log["blockNumber"], 16),
        "tx_hash": bytes.fromhex(log["transactionHash"][2:]),
        "log_index": int(log["logIndex"], 16),
        "from": bytes.fromhex(topics[1][-40:]),
        "to": bytes.fromhex(topics[2][-40:]),
        "amount": int(data, 16) if data not in ("0x", "") else 0,
        "block_ts": int(ts, 16) if ts else None,
    }


def fetch_logs_adaptive(client, address: str, topics: list, lo: int, hi: int,
                        log_cap: int, stats: dict) -> list[dict]:
    """eth_getLogs over [lo, hi]. If the node refuses the range, times out, or
    returns >= log_cap entries (possible silent truncation), split in two and
    retry. Returns all logs in block order. stats['splits'] counts splits."""
    out: list[dict] = []
    stack = [(lo, hi)]
    while stack:
        a, b = stack.pop()
        try:
            logs = client.get_logs(address, topics, a, b)
            if len(logs) >= log_cap and a < b:
                raise RangeTooLarge("log cap reached")
        except RangeTooLarge:
            if a >= b:
                raise RpcError(f"block {a} alone exceeds the node limits")
            mid = (a + b) // 2
            stats["splits"] = stats.get("splits", 0) + 1
            stack.append((mid + 1, b))  # right half processed after the left one
            stack.append((a, mid))
            continue
        out.extend(logs)
    out.sort(key=lambda l: (int(l["blockNumber"], 16), int(l["logIndex"], 16)))
    return out
