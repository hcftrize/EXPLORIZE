"""Minimal JSON-RPC client with throttling, retries and range-limit detection.

Security note: exceptions never contain the request URL (it embeds the API key).
"""

from __future__ import annotations

import random
import time

import requests


class RpcError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class RangeTooLarge(RpcError):
    """The node refused or could not serve this eth_getLogs range."""


_RATE_HINTS = ("rate limit", "rate-limit", "too many requests", "throttl", "compute units per second")
_RANGE_HINTS = (
    "range", "too many", "exceed", "limit", "more than", "10000", "10,000",
    "timeout", "timed out", "response size", "query returned", "too large",
)


def _classify(message: str, code: int | None) -> str:
    m = (message or "").lower()
    if code == 429 or any(h in m for h in _RATE_HINTS):
        return "rate"
    if any(h in m for h in _RANGE_HINTS):
        return "range"
    return "other"


class RpcClient:
    def __init__(self, url: str, *, timeout: float = 30.0, max_retries: int = 6,
                 batch_size: int = 3, max_rps: float = 20.0):
        self._url = url
        self.timeout = timeout
        self.max_retries = max_retries
        self.batch_size = max(1, batch_size)
        self._min_interval = 1.0 / max_rps if max_rps and max_rps > 0 else 0.0
        self._last = 0.0
        self._session = requests.Session()
        self.calls = 0

    # -- low level ---------------------------------------------------------
    def _throttle(self) -> None:
        if self._min_interval:
            wait = self._last + self._min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
        self._last = time.monotonic()

    def _post(self, payload, *, range_sensitive: bool = False):
        attempt = 0
        while True:
            self._throttle()
            self.calls += 1
            try:
                resp = self._session.post(self._url, json=payload, timeout=self.timeout)
            except requests.Timeout:
                if range_sensitive and attempt >= 1:
                    raise RangeTooLarge("timeout")
                err = "timeout"
            except requests.RequestException as e:  # never str(e): it contains the URL
                err = f"network error ({type(e).__name__})"
            else:
                if resp.status_code == 429:
                    err = "http 429"
                elif resp.status_code >= 500:
                    err = f"http {resp.status_code}"
                else:
                    try:
                        data = resp.json()
                    except ValueError:
                        raise RpcError(f"non-JSON answer (http {resp.status_code})")
                    return data
            attempt += 1
            if attempt > self.max_retries:
                raise RpcError(f"giving up after {self.max_retries} retries: {err}")
            time.sleep(min(30.0, (2 ** attempt) * 0.25) + random.random() * 0.25)

    def _unwrap(self, obj, *, range_sensitive: bool):
        """Return result of one JSON-RPC response object or raise."""
        if isinstance(obj, dict) and obj.get("error"):
            e = obj["error"]
            msg = str(e.get("message", "")) if isinstance(e, dict) else str(e)
            code = e.get("code") if isinstance(e, dict) else None
            kind = _classify(msg, code)
            if kind == "range" and range_sensitive:
                raise RangeTooLarge(msg, code)
            raise RpcError(msg, code)
        if not isinstance(obj, dict) or "result" not in obj:
            raise RpcError("malformed JSON-RPC answer")
        return obj["result"]

    def call(self, method: str, params: list, *, range_sensitive: bool = False):
        attempt = 0
        while True:
            data = self._post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                              range_sensitive=range_sensitive)
            try:
                return self._unwrap(data, range_sensitive=range_sensitive)
            except RangeTooLarge:
                raise
            except RpcError as e:
                if _classify(str(e), e.code) == "rate" and attempt < self.max_retries:
                    attempt += 1
                    time.sleep(min(30.0, (2 ** attempt) * 0.25) + random.random() * 0.25)
                    continue
                raise

    def batch(self, calls: list[tuple[str, list]]) -> list:
        """Run many calls, grouped in JSON-RPC batches. Returns results in order."""
        out: list = []
        for i in range(0, len(calls), self.batch_size):
            part = calls[i:i + self.batch_size]
            if len(part) == 1:
                out.append(self.call(part[0][0], part[0][1]))
                continue
            payload = [{"jsonrpc": "2.0", "id": j, "method": m, "params": p}
                       for j, (m, p) in enumerate(part)]
            data = self._post(payload)
            if not isinstance(data, list):
                raise RpcError("batch answer is not a list")
            by_id = {o.get("id"): o for o in data if isinstance(o, dict)}
            for j in range(len(part)):
                if j not in by_id:
                    raise RpcError("batch answer is missing an item")
                out.append(self._unwrap(by_id[j], range_sensitive=False))
        return out

    # -- helpers -----------------------------------------------------------
    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def chain_id(self) -> int:
        return int(self.call("eth_chainId", []), 16)

    def eth_call(self, to: str, data: str, block: str = "latest") -> str:
        return self.call("eth_call", [{"to": to, "data": data}, block])

    def get_code(self, address: str, block: int | str = "latest") -> str:
        b = block if isinstance(block, str) else hex(block)
        return self.call("eth_getCode", [address, b])

    def get_logs(self, address: str, topics: list, from_block: int, to_block: int) -> list:
        params = [{"address": address, "topics": topics,
                   "fromBlock": hex(from_block), "toBlock": hex(to_block)}]
        return self.call("eth_getLogs", params, range_sensitive=True)

    def get_block_timestamps(self, blocks) -> dict[int, int]:
        blocks = sorted(set(blocks))
        results = self.batch([("eth_getBlockByNumber", [hex(b), False]) for b in blocks])
        out: dict[int, int] = {}
        for b, r in zip(blocks, results):
            if not r or "timestamp" not in r:
                raise RpcError(f"block {b} not found")
            out[b] = int(r["timestamp"], 16)
        return out
