"""Read many contract views in one eth_call through Multicall3 (no extra dependency).

Multicall3 lives at the same address on every EVM chain we use. We only need aggregate3, with
allowFailure = true, so one reverting call (e.g. ownerOf of a burned NFT) never breaks the batch.

ABI reminders (everything is 32-byte words):
  aggregate3((address target, bool allowFailure, bytes callData)[] calls)
      -> (bool success, bytes returnData)[]
"""

from __future__ import annotations

from .rpc import RpcError

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SEL_AGGREGATE3 = "82ad56cb"

# Function selectors (verified against keccak256 of the signatures and the T-RIZE contract reference)
SEL_BALANCE_OF = "70a08231"         # balanceOf(address)
SEL_TOTAL_SUPPLY = "18160ddd"       # totalSupply()
SEL_OWNER_OF = "6352211e"           # ownerOf(uint256)                  BondNFT
SEL_BONDED = "c9836074"             # bonded(uint256)                   GovernanceBonding
SEL_BROKEN_BOND = "a6dd16f1"        # brokenBond(uint256)
SEL_VESTED = "968fb8ba"             # vested(uint256)
SEL_GET_BOND_RELEASE = "2e7d1223"   # getBondRelease(uint256) -> (uint256 amount, uint256 time)[]
SEL_GET_SCHEDULE = "c5ca93a7"       # getSchedule(uint256) -> (amount, cliff, vesting, start)
SEL_GET_VESTING_INITIATOR = "716b55f8"
SEL_RELEASABLE = "1c269043"         # releasableTokens(uint256)
SEL_GET_RELEASE_WARMUP = "5f0af798"  # getReleaseWarmup()

WORD = 32


def _pad(b: bytes) -> bytes:
    return b + b"\x00" * (-len(b) % WORD)


def enc_uint(n: int) -> bytes:
    return int(n).to_bytes(WORD, "big")


def enc_address(a: str) -> bytes:
    h = a[2:] if a.startswith("0x") else a
    if len(h) != 40:
        raise ValueError(f"bad address {a!r}")
    return b"\x00" * 12 + bytes.fromhex(h)


def call_uint(selector: str, n: int) -> bytes:
    """calldata of f(uint256)."""
    return bytes.fromhex(selector) + enc_uint(n)


def call_address(selector: str, a: str) -> bytes:
    """calldata of f(address)."""
    return bytes.fromhex(selector) + enc_address(a)


def call_none(selector: str) -> bytes:
    """calldata of f()."""
    return bytes.fromhex(selector)


# -- aggregate3 encoding / decoding -------------------------------------------------------
def encode_aggregate3(calls: list[tuple[str, bytes]]) -> str:
    """calls: [(target 0x..., calldata bytes)] -> 0x calldata for aggregate3 (allowFailure = true)."""
    tuples = []
    for target, data in calls:
        tuples.append(enc_address(target) + enc_uint(1) + enc_uint(3 * WORD)
                      + enc_uint(len(data)) + _pad(data))
    head = b""
    offset = WORD * len(tuples)  # offsets are relative to the start of the elements area
    for t in tuples:
        head += enc_uint(offset)
        offset += len(t)
    body = enc_uint(WORD) + enc_uint(len(tuples)) + head + b"".join(tuples)
    return "0x" + SEL_AGGREGATE3 + body.hex()


def _word(b: bytes, pos: int) -> int:
    if pos < 0 or pos + WORD > len(b):
        raise ValueError("answer too short")
    return int.from_bytes(b[pos:pos + WORD], "big")


def decode_aggregate3(hex_answer: str) -> list[tuple[bool, bytes]]:
    b = bytes.fromhex(hex_answer[2:] if hex_answer.startswith("0x") else hex_answer)
    arr = _word(b, 0)
    n = _word(b, arr)
    base = arr + WORD
    out = []
    for i in range(n):
        t = base + _word(b, base + WORD * i)
        ok = _word(b, t) != 0
        boff = _word(b, t + WORD)
        blen = _word(b, t + boff)
        start = t + boff + WORD
        if start + blen > len(b):
            raise ValueError("answer too short")
        out.append((ok, b[start:start + blen]))
    return out


def multicall(client, calls: list[tuple[str, bytes]], block: str = "latest",
              *, group: int = 400, min_group: int = 10, log=None) -> list[tuple[bool, bytes]]:
    """Run `calls` through Multicall3 in groups; a group the node refuses is halved and retried."""
    out: list[tuple[bool, bytes]] = []
    i = 0
    size = max(1, group)
    while i < len(calls):
        part = calls[i:i + size]
        try:
            raw = client.eth_call(MULTICALL3, encode_aggregate3(part), block)
            res = decode_aggregate3(raw)
            if len(res) != len(part):
                raise RpcError("multicall answered the wrong number of results")
        except (RpcError, ValueError) as e:
            if size > min_group:
                size = max(min_group, size // 2)
                if log:
                    log(f"  multicall group refused ({type(e).__name__}); retrying with {size} calls")
                continue
            raise
        out.extend(res)
        i += len(part)
    return out


# -- result decoders ----------------------------------------------------------------------
def dec_uint(b: bytes) -> int:
    return _word(b, 0)


def dec_address(b: bytes) -> str:
    return "0x" + b[12:32].hex()


def dec_release_list(b: bytes) -> list[tuple[int, int]]:
    """getBondRelease -> [(amount, time)]; empty queue = no entries."""
    if len(b) < 2 * WORD:
        return []
    off = _word(b, 0)
    n = _word(b, off)
    if n > 64:
        raise ValueError(f"implausible release queue length {n}")
    base = off + WORD
    return [(_word(b, base + 2 * WORD * k), _word(b, base + 2 * WORD * k + WORD)) for k in range(n)]


def dec_schedule(b: bytes) -> dict:
    """getSchedule -> amount, cliff, vesting (seconds), start (timestamp)."""
    return {"amount": _word(b, 0), "cliff": _word(b, WORD), "vesting": _word(b, 2 * WORD),
            "start": _word(b, 3 * WORD)}
