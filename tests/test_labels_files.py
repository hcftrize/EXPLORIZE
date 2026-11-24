"""The labels files you edit by hand: catch a typo before it reaches the database."""
import re
from collections import Counter

from indexer.registry import Registry

ADDR = re.compile(r"^0x[0-9a-f]{40}$")
CATEGORIES = {"dex", "aggregator", "bridge", "proxy", "liquidity-manager", "account-abstraction", "cex", "market-maker", "team",
              "wallet", "system", "other"}
CHAINS = {"all", "base", "ethereum", "bsc", "polygon"}


def test_entities_are_well_formed_and_unique():
    ents = Registry.from_dir().entities()
    assert not [k for k, n in Counter((e["chain"], e["address"]) for e in ents).items() if n > 1]
    for e in ents:
        assert ADDR.match(e["address"]), e
        assert e["chain"] in CHAINS, e
        assert e["category"] in CATEGORIES, e
        assert e["label"].strip(), e
    # an address must not carry two labels (one for "all" and one for a chain): the page would pick one of them at random
    by_addr = Counter(e["address"] for e in ents)
    for a, n in by_addr.items():
        if n > 1:
            chains = [e["chain"] for e in ents if e["address"] == a]
            assert "all" not in chains, f"{a} is labelled for 'all' and for {chains}"


def test_pools_point_at_known_venues_and_are_not_also_labelled():
    reg = Registry.from_dir()
    for p in reg.pools():
        assert p.venue in reg.venues, p
        assert p.chain in CHAINS - {"all"}, p
        assert reg.entity(p.chain, p.key) is None, f"{p.key} is both a pool and an entity label"
    for chain, key in reg.not_pools:
        assert reg.pool(chain, key) is None


def test_pool_names_are_unique_per_venue_name():
    reg = Registry.from_dir()
    names = Counter(p.name for p in reg.pools())
    assert not [n for n, c in names.items() if c > 1]
