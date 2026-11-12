"""Configuration: chain list, RPC URLs and database URL.

Secrets only ever come from environment variables:
  DRPC_API_KEY   dRPC key (builds https://lb.drpc.live/<slug>/<key>)
  DATABASE_URL   Postgres connection string (use the DIRECT one, not "-pooler")
Optional:
  RPC_URL_<SLUG> full RPC URL override for one chain (e.g. RPC_URL_BASE)
  RPC_MAX_RPS    max requests per second (default 20; dRPC free tier allows 25)
  RPC_BATCH_SIZE JSON-RPC batch size (default 3; dRPC free tier allows 3)
  LOG_CAP        max logs per eth_getLogs answer before we split (default 10000)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHAINS_FILE = ROOT / "config" / "chains.json"
SCHEMA_FILE = ROOT / "db" / "schema.sql"


@dataclass(frozen=True)
class Chain:
    key: str
    chain_id: int
    name: str
    drpc_slug: str
    token: str            # lowercase 0x address
    start_block: int | None
    reorg_margin: int
    initial_chunk: int
    min_chunk: int
    max_chunk: int

    @property
    def token_bytes(self) -> bytes:
        return bytes.fromhex(self.token[2:])


def load_chains() -> dict[str, Chain]:
    raw = json.loads(CHAINS_FILE.read_text())
    chains: dict[str, Chain] = {}
    for key, c in raw["chains"].items():
        token = c["token"].lower()
        if not (token.startswith("0x") and len(token) == 42):
            raise ValueError(f"{key}: invalid token address {c['token']!r}")
        chains[key] = Chain(
            key=key,
            chain_id=int(c["chain_id"]),
            name=c["name"],
            drpc_slug=c["drpc_slug"],
            token=token,
            start_block=c.get("start_block"),
            reorg_margin=int(c["reorg_margin"]),
            initial_chunk=int(c["initial_chunk"]),
            min_chunk=int(c["min_chunk"]),
            max_chunk=int(c["max_chunk"]),
        )
    return chains


def select_chains(selector: str) -> list[Chain]:
    chains = load_chains()
    if selector == "all":
        return list(chains.values())
    if selector not in chains:
        raise SystemExit(f"Unknown chain {selector!r}. Choose one of: all, {', '.join(chains)}")
    return [chains[selector]]


def rpc_url(chain: Chain) -> str:
    override = os.environ.get(f"RPC_URL_{chain.key.upper()}")
    if override:
        return override
    key = os.environ.get("DRPC_API_KEY")
    if not key:
        raise SystemExit("DRPC_API_KEY is not set (and no RPC_URL_%s override)." % chain.key.upper())
    return f"https://lb.drpc.live/{chain.drpc_slug}/{key}"


def database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL is not set.")
    return url


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default
