"""The registry: which addresses and pool ids are RIZE trading venues, and what the tokens around them are.

Everything is read from the JSON files of the `labels/` folder (the files the owner maintains by hand) and, at run time,
completed with the pools the code discovered itself (named "POOL UNKNW n" until the owner names them).

Facts are matched by ADDRESS or pool id, never by symbol: impostor tokens called "RIZE" exist.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

NATIVE = "0x" + "00" * 20                      # native coin (ETH, BNB, POL) in a pool's currency list
LABELS_DIR = Path(os.environ.get("LABELS_DIR") or Path(__file__).resolve().parent.parent / "labels")

KINDS = ("v3", "v2", "v4", "infinity", "rfq")
SINGLETON_KINDS = ("v4", "infinity")           # many pools inside one manager contract, found by pool id


@dataclass(frozen=True)
class Pool:
    chain: str
    key: str                                   # pool address (0x + 40 hex) or pool id (0x + 64 hex), lower case
    name: str
    venue: str
    kind: str
    token0: str = ""
    token1: str = ""
    fee: int | None = None
    tick_spacing: int | None = None
    factory: str = ""
    gauge: str = ""
    manager: str = ""                          # contract that emits the events of a singleton design
    holder: str = ""                           # address that holds the tokens when it is not the key (vault / manager)
    status: str = "active"                     # active | historic
    source: str = "manual"                     # manual (labels folder) | auto (found by the code)
    note: str = ""

    @property
    def is_id(self) -> bool:
        return len(self.key) == 66

    @property
    def holder_addr(self) -> str:
        """The address whose token balance IS this pool's balance."""
        return self.holder or (self.manager if self.is_id else self.key)

    def counter_token(self, rize: str) -> str:
        return self.token1 if self.token0 == rize else self.token0


def _lower(v: str | None) -> str:
    return (v or "").lower()


def _read(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Registry:
    def __init__(self, tokens: dict, venues: dict, pools: list[Pool], entities: list[dict]):
        self._tokens = tokens
        self.venues = venues.get("venues", {})
        self.factories = {k.lower(): v for k, v in venues.get("factories", {}).items()}
        self._pools: dict[tuple[str, str], Pool] = {(p.chain, p.key): p for p in pools}
        self._entities = {(e["chain"], e["address"].lower()): e for e in entities}

    # ------------------------------------------------------------------ loading
    @classmethod
    def from_dir(cls, path: str | Path | None = None) -> "Registry":
        d = Path(path) if path else LABELS_DIR
        tokens = _read(d / "tokens.json")
        venues = _read(d / "venues.json")
        raw = _read(d / "pools.json").get("pools", [])
        entities = _read(d / "entities.json").get("entities", [])
        pools = []
        for p in raw:
            pools.append(Pool(
                chain=p["chain"], key=_lower(p["key"]), name=p["name"], venue=p.get("venue", ""), kind=p["kind"],
                token0=_lower(p.get("token0")), token1=_lower(p.get("token1")), fee=p.get("fee"),
                tick_spacing=p.get("tick_spacing"), factory=_lower(p.get("factory")), gauge=_lower(p.get("gauge")),
                manager=_lower(p.get("manager")), holder=_lower(p.get("holder")), status=p.get("status", "active"),
                source=p.get("source", "manual"), note=p.get("note", "")))
        return cls(tokens, venues, pools, entities)

    # ------------------------------------------------------------------ tokens
    def _chain_tokens(self, chain: str) -> dict:
        return self._tokens.get(chain, {})

    def rize(self, chain: str) -> str:
        return _lower(self._chain_tokens(chain).get("rize"))

    def wrapped(self, chain: str) -> str:
        return _lower(self._chain_tokens(chain).get("wrapped"))

    def native(self, chain: str) -> tuple[str, int]:
        n = self._chain_tokens(chain).get("native", {"symbol": "ETH", "decimals": 18})
        return n["symbol"], int(n["decimals"])

    def stables(self, chain: str) -> set[str]:
        return {a.lower() for a in self._chain_tokens(chain).get("stables", {})}

    def fake_rize(self, chain: str) -> set[str]:
        return {a.lower() for a in self._chain_tokens(chain).get("fake_rize", [])}

    def token_meta(self, chain: str) -> dict[str, tuple[str, int]]:
        """address -> (symbol, decimals) for every token we know; RIZE is 18 decimals, the native coin sits at the zero address."""
        t = self._chain_tokens(chain)
        out: dict[str, tuple[str, int]] = {}
        for src in ("tokens", "stables"):
            for a, v in t.get(src, {}).items():
                out[a.lower()] = (v["symbol"], int(v["decimals"]))
        if t.get("rize"):
            out[t["rize"].lower()] = ("RIZE", 18)
        sym, dec = self.native(chain)
        out[NATIVE] = (sym, dec)
        return out

    # ------------------------------------------------------------------ pools
    def pools(self, chain: str | None = None) -> list[Pool]:
        return [p for (c, _), p in self._pools.items() if chain in (None, c)]

    def pool(self, chain: str, key: str) -> Pool | None:
        return self._pools.get((chain, _lower(key)))

    def add_pool(self, pool: Pool) -> None:
        self._pools[(pool.chain, pool.key)] = pool

    def holders(self, chain: str) -> dict[str, list[Pool]]:
        """holder address -> pools whose tokens it holds (address pools hold their own; singleton pools share one holder)."""
        out: dict[str, list[Pool]] = {}
        for p in self.pools(chain):
            out.setdefault(p.holder_addr, []).append(p)
        return out

    def rfq_addresses(self, chain: str) -> set[str]:
        return {p.key for p in self.pools(chain) if p.kind == "rfq"}

    def singleton_ids(self, chain: str) -> dict[str, Pool]:
        return {p.key: p for p in self.pools(chain) if p.kind in SINGLETON_KINDS}

    def next_unknown_number(self) -> int:
        best = 0
        for p in self._pools.values():
            if p.name.startswith("POOL UNKNW "):
                tail = p.name.split(" ")[2] if len(p.name.split(" ")) > 2 else "0"
                if tail.isdigit():
                    best = max(best, int(tail))
        return best + 1

    def register_unknown(self, chain: str, key: str, kind: str = "v2", token0: str = "", token1: str = "",
                         factory: str = "") -> Pool:
        """A pool the code is certain about (it emits swap events and moves RIZE) that the owner has not named yet."""
        hint = self.factories.get(_lower(factory), {})
        p = Pool(chain=chain, key=_lower(key), name=f"POOL UNKNW {self.next_unknown_number()}",
                 venue=hint.get("venue", ""), kind=kind, token0=_lower(token0), token1=_lower(token1),
                 factory=_lower(factory), source="auto", note=hint.get("note", ""))
        self.add_pool(p)
        return p

    # ------------------------------------------------------------------ labels
    def label(self, chain: str, address: str) -> str | None:
        a = _lower(address)
        e = self._entities.get((chain, a)) or self._entities.get(("all", a))
        if e:
            return e["label"]
        p = self.pool(chain, a)
        return p.name if p else None

    def entities(self) -> list[dict]:
        return list(self._entities.values())

    def entity(self, chain: str, address: str) -> dict | None:
        a = _lower(address)
        return self._entities.get((chain, a)) or self._entities.get(("all", a))

    def with_names_from(self, rows: list[dict]) -> None:
        """Merge pools stored in the database (auto-discovered ones, or renamed) without overriding the labels files."""
        for r in rows:
            key = (r["chain"], r["key"])
            if key not in self._pools:
                self._pools[key] = Pool(**r)
