# Labels, reviews and corrections: the 4 steps

Do these four steps in this order, every time. Names and corrections of the past are two different things.

## 1. Find what is unknown (Neon SQL editor)

Open `labels/review_queries.sql` and run the queries one at a time (they only read):

| Query | What it shows |
|---|---|
| A1, A2 | busiest addresses / biggest holders with no label |
| B1, B2, B3 | routers, relayers, signers, traders, liquidity providers with no label |
| C1, C2 | what waits for review, by reason and by explanation |
| D1, D2 | unknown pool ids (to add to `labels/pools.json`), pools found by the code and still unnamed |
| E1, E2 | transactions whose venue was guessed, crashed transactions |
| F1, F2 | trades with an implausible price, trades by flag |
| G | everything stored about one transaction |

Anomalies (supply, CCIP) are in `anomalies/ANOMALIES.txt`.

## 2. Review by hand and write down your findings

For each finding keep: address (or pool id), chain, name, category, and for a pool its venue. Send the list to Claude, or edit the files yourself:

- `labels/entities.json`: `{"address": "0x...", "chain": "base", "label": "Name", "category": "cex", "source": "where you found it", "logo": ""}` (chain: `all`, `base`, `ethereum`, `bsc` or `polygon`; categories: dex, aggregator, bridge, proxy, liquidity-manager, account-abstraction, cex, team, wallet, system, other)
- `labels/pools.json`: one entry per pool: `chain`, `key` (address, or pool id for Uniswap V4 / PancakeSwap Infinity), `name`, `venue`, `kind` (v3, v2, v4, infinity, rfq), tokens, fee, `status`
- `labels/venues.json`: a new venue (short name, kind, logo)

Then commit to `main`. Check that the JSON is valid (GitHub shows a red mark on a syntax error).

## 3. Names appear by themselves

At the next unified run (every 10 minutes) the files are reloaded and every table, wallet page and Top Holders row shows the new names, past included. Nothing to recompute for a plain name.

## 4. Fix the past (only when the label changes how a transaction is READ)

A name never needs this. These cases do: a pool or pool id added to `pools.json` (its old transactions are stored as "unknown pool" or "guess"), a router / relayer / venue that changes who the trader is, a transaction you want read again. Run **Actions > Explorize fix labels**:

1. First with `dry_run` ticked: it says how many transactions would be re-read.
2. Then without `dry_run`. It re-reads only the selected transactions and rewrites their trades, liquidity moves and review rows, then prints what changed ("became trades", "left review", "waiting for review: guess 40 -> 12").

Fields (all optional, they add up): `reasons` (review queue), `pools` (address or id, the first 18 characters of an id are enough), `addresses` (routers / relayers / traders), `flags` (for example `price-implausible`), `txs` (hashes), `use_file` (the list in `corrections/transactions.txt`). A transaction that stays a guess is normal when nothing proves its venue: a trade needs a Swap event the code recognises.
