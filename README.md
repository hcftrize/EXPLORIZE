# Explorize

Open-source indexer behind **Explorize**, the RIZE token explorer of the
[Tokerize](https://github.com/hcftrize/TOKERIZE) community project.

It copies every RIZE ERC-20 `Transfer` event from **Base, Ethereum, BNB Chain and
Polygon** into a Postgres database, from the token deployment block to the chain
head, and keeps it up to date. The website reads that database through a small
API; this repository only contains the indexer, the schema and its automation.

```
chains (dRPC RPC)  ->  indexer (GitHub Actions, Python)  ->  Postgres (Neon)  ->  API + page (TOKERIZE repo)
```

## How it stays correct

- `eth_getLogs` ranges are split automatically when a node refuses them, times
  out, or returns as many logs as the cap (possible silent truncation).
- Each block range is saved in **one transaction** with its checkpoint:
  delete the range, insert fresh rows, move the checkpoint. Re-running a range is harmless.
- Every sync restarts a few blocks **before** the checkpoint (`reorg_margin` in
  `config/chains.json`), so reorganised blocks are rewritten.
- `verify` rebuilds the total supply from our transfers (mints minus burns) and
  compares it with `totalSupply()` on-chain.
- One indexer per chain at a time (Postgres advisory lock).

## Setup

Repository secrets (**Settings > Secrets and variables > Actions**):

| Secret | Value |
|---|---|
| `DATABASE_URL` | Neon connection string, **direct** one (host without `-pooler`) |
| `DRPC_API_KEY` | your dRPC key |

## Workflows (Actions tab, all started manually or through the GitHub API)

| Workflow | What it does |
|---|---|
| **Unified update explorize** | **The one that keeps the site alive.** Updates everything the dashboard shows at the same block, in one transaction (see [How everything runs](#how-everything-runs)). Started every 10 minutes by an outside timer. |
| **Explorize probe** | Checks key, contracts, real `eth_getLogs` limits, archive support and the database. Tick `init_db` the first time to create the tables. |
| **Explorize backfill** | One-off full history of the 4 chains in parallel. Resumable: run it again if it stops. Needs an archive node (dRPC Pay As You Go). |
| **Explorize sync** | Transfers only. Used for the first sync and for catching up after a long stop; the unified update does this step by itself in normal life. |
| **Explorize market layer** | Turns the synced transfers into trades and liquidity moves (`enrich`), photographs the pools (`snapshot`), finds unlisted RIZE pools (`discover`), reconciles the bridges with the Chainlink CCIP API (`ccip`, `ccip-gaps`), repairs crashed transactions (`repair`) and prints a `report`. Resumable. Used for big catch-ups; the unified update does the same work in small steps. |
| **Explorize fix labels** | Corrects the past after a label change: re-reads only the transactions you point at (review queue, a new pool, a router, a flag, a list of hashes) and rewrites their trades and review rows. `dry_run` first. See `labels/WORKFLOW.md`. |
| **Explorize holders** | Full rebuild of the Top Holders ranking, by hand. The unified update already rebuilds it once a day. |
| **Explorize verify** | Supply integrity check and per-chain status (also balances against `balanceOf()`). Do not run it during a sync. |
| Explorize balance, inspect, market check, governance check | Read-only diagnostics (a balance at chosen blocks, window-width comparison, how a transaction is read, the governance contract). They write nothing. |

## Run locally

```bash
pip install -r requirements-dev.txt
export DATABASE_URL=...  DRPC_API_KEY=...
python -m indexer init-db
python -m indexer probe
python -m indexer sync --chain base --max-minutes 10
python -m indexer verify
python -m indexer status
```

Tests need a throwaway Postgres: `TEST_DATABASE_URL=postgresql://... pytest`.

## Configuration

`config/chains.json` holds the contract per chain, the reorg margin and chunk sizes.
Optional environment variables: `RPC_URL_<CHAIN>` (full RPC URL override),
`RPC_MAX_RPS` (default 20), `RPC_BATCH_SIZE` (default 3), `LOG_CAP` (default 10000).
Secrets are never printed: errors never contain RPC URLs.

## Contracts

| Chain | RIZE |
|---|---|
| Base | `0x9818B6c09f5ECc843060927E8587c427C7C93583` |
| Ethereum | `0x9F1E8F87c6321b84baD7DDa7DfB86D5115A47605` |
| BNB Chain | `0xaedaff046601beb063b647845dfb21841f32d6a4` |
| Polygon | `0x9F1E8F87c6321b84baD7DDa7DfB86D5115A47605` |

---

# How everything runs

This part explains, in plain words, what happens from the moment a timer fires to the moment a number changes on the Explorize page, and then lists every file of the repository with what it does.

## The big picture

```
 cron-job.org            GitHub Actions                Neon (Postgres)            Vercel (TOKERIZE repo)
 (timer, every   ──►  "Unified update explorize"  ──►  one transaction,   ──►   /api/explorize/*   ──►  explorize.html
  10 minutes)          runs python -m indexer unified   committed at the end      reads the database      the page people see
       │                      │       ▲
       │                      │       └── dRPC: the 4 blockchains (Base, Ethereum, BNB Chain, Polygon)
       │                      └────────── Chainlink CCIP API (bridge messages)
       └─ one HTTP call: POST …/actions/workflows/unified-update-explorize.yml/dispatches   {"ref":"main"}
```

Three ideas to keep in mind:

1. **A timer outside GitHub starts everything.** GitHub's own schedule is too slow and too irregular, so `cron-job.org` calls the GitHub API every 10 minutes. The workflow has no `schedule:` of its own on purpose.
2. **One run updates everything, at the same block.** Transfers, trades, liquidity, bridges, balances and the Top Holders table are all computed for the same block of each chain, inside one database transaction. The website only sees the result once that transaction is committed.
3. **If anything is not aligned or fails, nothing is published.** The site keeps showing the last good state, untouched. Differences that are only worth a human's attention (an *anomaly*) never block the publication: they are written to `anomalies/ANOMALIES.txt`.

## What happens every 10 minutes, step by step

| # | Who | What happens |
|---|---|---|
| 1 | cron-job.org | Sends `POST https://api.github.com/repos/hcftrize/EXPLORIZE/actions/workflows/unified-update-explorize.yml/dispatches` with the body `{"ref":"main"}`, a fine-grained token (this repository only, permission *Actions: Read and write*) in the `Authorization: Bearer …` header, and a `User-Agent` header. GitHub answers `204` when it accepted the call. |
| 2 | GitHub Actions | Starts the workflow **Unified update explorize** on a fresh machine: installs Python and `requirements.txt`, then runs `python -u -m indexer unified --max-minutes 25`. Two runs never overlap: a new start waits for the running one (and a waiting one is replaced by the newest). |
| 3 | The indexer | Opens **one database transaction** (`StagedConnection` in `indexer/db.py`) and takes a lock so that no other unified run writes at the same time. |
| 4 | The indexer | Does the seven steps of the next table, all inside that transaction. |
| 5 | The indexer | Checks that everything is aligned. **If yes: COMMIT** (the log says `PUBLISHED`). **If not, or on any error: ROLLBACK** (the log says `NOT PUBLISHED`) and the previous state stays online. |
| 6 | The workflow's last step | Commits `anomalies/ANOMALIES.txt` if the run found new anomalies. |
| 7 | The website | Every API call reads the database as usual. Before the COMMIT it sees the old state, after it the new one. There is no moment where it sees half of the update. |

A typical run with nothing new takes about **30 seconds**. A run that finds new transactions takes a little longer. A run is cut at 25 minutes and, in that case, publishes nothing.

### The seven steps inside one run

| Step | What it does | If something is wrong |
|---|---|---|
| 1. Transfers | Every chain is synced to its head: new RIZE `Transfer` events are copied into `transfers` (same code as the `sync` command, restarting a few blocks before the checkpoint to absorb reorganisations). | A chain that is not at its head, or a sync cut by the time budget, **blocks** the run. |
| 2. Supply check | Rebuilds the supply from our transfers (mints minus burns) and compares it with `totalSupply()` read on-chain at the synced block, per chain. | A difference is an **anomaly**, the run goes on. |
| 3. Market layer | For the new transactions that touch a known pool or venue: fetches the receipt and turns it into a trade or a liquidity move (`trades`, `liquidity_events`). What cannot be proven goes to `enrich_review`. Crashed transactions are retried. Pools are photographed once an hour (`pool_snapshots`). Then the unknown tokens seen in trades are named from their contracts (`tokens`, never blocks). | Trades behind the transfers **block** the run. Unreadable transactions are an **anomaly**. |
| 4. CCIP bridges | Burns and mints are looked up in the Chainlink CCIP API and every bridge message is reconciled (what was burned on one chain is minted on the other). | A burn or mint old enough to have a message but not yet looked up **blocks** the run (messages still in flight are fine). Messages that do not reconcile are an **anomaly**. |
| 5. Balances | Reads the live `balanceOf()` of every address touched since the last run, at exactly the synced block, and keeps them in `holder_balances`. | Balances that do not add up to `totalSupply()` are an **anomaly**. |
| 6. Top Holders | **Once a day** (24 h by default, `RANKING_EVERY_HOURS`): reads every governance bond on Base, adds the held balances and rebuilds the ranking (`holders`, `bonds`, `holders_meta`). The other runs leave the published table untouched; the page shows its own age (« updated 17h ago »). | A failed read **blocks** the run. Governance differences are an **anomaly**. |
| 7. Checks and publish | Verifies the alignment of every table, writes the run in `unified_runs`, then commits. | Misalignment **blocks**; nothing half-done is ever published. |

### What blocks and what does not

| Blocks the publication (the site keeps the last good state) | Only writes an anomaly (data is published anyway) |
|---|---|
| A chain not at its head, or stopped early | Supply from our transfers different from `totalSupply()` |
| Trades / liquidity not computed up to the synced block | Balances that do not add up to `totalSupply()` |
| A CCIP burn or mint that should have been looked up and was not | CCIP messages that do not reconcile |
| Balances or Top Holders not written for the same blocks | Transactions the market layer could not read |
| An RPC error, a crash, a killed run, the 25-minute limit | Governance contract differences |

When a run is blocked, the reason is written as one line in `anomalies/ANOMALIES.txt` (`NOT PUBLISHED`), and the next run simply tries again.

### How to read the log of a run

- `PUBLISHED. {...}` and `"seconds": 31.1` — success; the JSON lists, per chain, the block reached, the head, the lag (0) and the new transfers.
- `NOT PUBLISHED (the site keeps the last good state): <reason>` — nothing changed on the site; the reason says why (chain behind, CCIP backlog, RPC error...).
- `SKIPPED: another unified run is in progress` — a manual correction or another run held the lock; the next run does the work.
- `Top Holders table: kept as published (the published table is 0.5 h old …)` — normal outside the daily rebuild.

## Things that run on a different rhythm

| What | When | How |
|---|---|---|
| Names, logos, pools, venues from `labels/*.json` | Reloaded at **every** run | Copied into `labels`, `pools`, `venues`, `tokens`; the API joins names at read time, so a new name shows everywhere, past included, at the next run. |
| Top Holders table | Once a day | Inside the unified run (step 6). A full manual rebuild is the workflow **Explorize holders**. |
| Pool balance photos | Once an hour | Inside step 3. |
| Fixing the past after a label change | When you decide | Workflow **Explorize fix labels** (see `labels/WORKFLOW.md`): re-reads only the transactions you point at. It writes each batch under the same lock as the unified run, so the two never write the same rows at once. |
| First backfill, long catch-up after days offline | Rarely | The commands `sync`, `enrich`, `ccip`, `holders` (workflows **Backfill**, **Sync**, **Market layer**) commit as they go. Start the unified update once they are up to date. |

## The database, table by table (Neon Postgres, `db/schema.sql`)

| Table | What it holds | Written by | Read by |
|---|---|---|---|
| `chain_state` | Per chain: first block, last synced block, block up to which balances are exact | sync, unified | status API |
| `transfers` | Every RIZE `Transfer` event of the 4 chains | sync, unified | transfers, wallet, tx APIs |
| `labels` | Names for addresses and pools (wallets, exchanges, bridges, routers…) | `labels/entities.json` and `pools.json`, at every run | all APIs (names) |
| `pools`, `venues`, `tokens` | The trading pools, venues and tokens known to the market layer. A token not in `labels/tokens.json` is named from its own contract (`source = auto`) | `labels/*.json`, the pools found automatically (`POOL UNKNW n`), `tokens.py` | market API |
| `trades` | One row per proven trade: side, trader, router, RIZE, USD, price, flags | enrich, reprocess | market, transfers APIs |
| `liquidity_events` | Liquidity adds, removes and fee collections per pool | enrich, reprocess | market API |
| `pool_snapshots` | What every pool holds, once an hour | enrich (`snapshot`) | market API |
| `enrich_state` | Checkpoint of the market layer per chain | enrich | enrich |
| `enrich_review` | Transactions that could not be proven, with the reason (`guess`, `flag`, `unknown_pool`, `crash`) | enrich, reprocess | you (SQL), repair |
| `ccip_messages`, `ccip_checked` | Chainlink CCIP bridge messages; burns already looked up | ccip | transfers, market APIs |
| `holder_balances` | Live balance of every address, read on-chain at the synced block (not displayed) | unified | supply/balance checks |
| `holders`, `holders_meta`, `bonds` | Top Holders ranking, its date and block, and every governance bond | unified, holders | holders, wallet APIs |
| `entity_holdings`, `entity_members` | The Entities tab: what each known entity (Kraken, MEXC, Aerodrome…) holds, all its addresses added up, and the addresses behind each | unified, holders (same transaction as `holders`) | holders API (`view=entities`, `view=entity`) |
| `unified_runs` | Journal of every unified run (ok or failed, summary) | unified | status API (« last update ») |
| `prices_hourly` | Reserved for a price history, not used yet | — | — |

## Setup of the heartbeat (once)

1. **Secrets** in the GitHub repository (*Settings > Secrets and variables > Actions*): `DATABASE_URL` (Neon, the **direct** connection string, host without `-pooler`) and `DRPC_API_KEY`. Optional repository *variable* `STORAGE_LIMIT_MB` (the workflows default to 4500, for a 5 GB Neon plan): the market layer stops writing above it.
2. **A fine-grained GitHub token** restricted to this repository with *Actions: Read and write* (Metadata read-only is added automatically).
3. **A job on cron-job.org**: URL `https://api.github.com/repos/hcftrize/EXPLORIZE/actions/workflows/unified-update-explorize.yml/dispatches`, method `POST`, body `{"ref":"main"}`, headers `Accept: application/vnd.github+json`, `Authorization: Bearer <token>`, `Content-Type: application/json`, `User-Agent: explorize-cron`, every 10 minutes. The test run must answer `204`.
4. Keep the token private: it is only ever typed into cron-job.org, never into a file of the repository.

## If something looks wrong

| What you see | What it means | What to do |
|---|---|---|
| cron-job.org answers `403` | The token lacks *Actions: Read and write*, targets another repository, or the `User-Agent` header is missing. | Fix the token or the headers; a good call answers `204`. |
| cron-job.org answers `404` | Wrong URL or workflow file name. | Compare with the URL above. |
| A run ends with `NOT PUBLISHED` | Something was not aligned or failed; the site is unchanged. | Read the reason in the log (and the last line of `ANOMALIES.txt`); the next run retries. Repeated CCIP backlog: run the **Market layer** workflow with `ccip` until it reports 0. |
| The page shows old data | The last runs were blocked, or the timer stopped. | Check the Actions tab for recent `PUBLISHED` runs and the cron-job.org history. |
| A name is missing for an address | It is not in `labels/entities.json` yet. | Add it, merge, wait for the next run. |
| Old trades are still « unknown pool » after adding a pool | Names update by themselves, the *reading* of past transactions does not. | Run **Explorize fix labels** (`dry_run` first). |

---

# Every file, and what it does

Folders are shown from the root of this repository. The website part lives in the other repository (TOKERIZE) and is listed at the end.

## Root

| File | Folder | What it does in practice |
|---|---|---|
| `README.md` | `/` | This page. |
| `requirements.txt` | `/` | The two Python libraries the indexer needs (`requests`, `psycopg2-binary`). The workflows install it before every run. |
| `requirements-dev.txt` | `/` | Same plus `pytest`, for running the tests on your own machine. |

## Workflows (`.github/workflows/`)

| File | Name in the Actions tab | What it does in practice |
|---|---|---|
| `unified-update-explorize.yml` | Unified update explorize | **The heartbeat.** Runs `python -m indexer unified` (25-minute budget, 75-minute safety limit). Started every 10 minutes by cron-job.org through the GitHub API, never overlaps itself, and its last step commits `anomalies/ANOMALIES.txt`. |
| `fix-labels-explorize.yml` | Explorize fix labels | After you changed labels or pools: re-reads only the transactions you select and rewrites their trades and review rows. `dry_run` is ticked by default. |
| `enrich.yml` | Explorize market layer | Manual menu for the market layer: `enrich`, `snapshot`, `discover`, `ccip`, `ccip-gaps`, `repair`, `tokens`, `report`. Used for big catch-ups and diagnoses. |
| `sync.yml` | Explorize sync | Transfers only (first sync, or catching up after a long stop). |
| `backfill.yml` | Explorize backfill | One-off full history of the 4 chains in parallel, from the token deployment blocks. |
| `holders.yml` | Explorize holders | Manual full rebuild of the Top Holders ranking from the chains. |
| `verify.yml` | Explorize verify | Rebuilds supply and balances from our transfers and compares them with the chains. |
| `probe.yml` | Explorize probe | Health check of the key, the 4 contracts, the RPC limits and the database; `init_db` creates the tables the first time. |
| `balance.yml` | Explorize balance | Diagnostic: a wallet balance and the total supply on-chain at chosen blocks. |
| `inspect.yml` | Explorize inspect | Diagnostic: reads the same blocks with several window widths and compares with the database. |
| `market-check.yml` | Explorize market check | Diagnostic, read-only: how a given transaction is read (`trade-check`), or what the busiest addresses are (`classify`). |
| `gov-check.yml` | Explorize governance check | Diagnostic, read-only: checks the governance contract state that the Top Holders job relies on. |

## The indexer (`indexer/`)

Everything is started with `python -m indexer <command>`; `__main__.py` is the front door.

| File | What it does in practice |
|---|---|
| `__main__.py` | The command line. Commands: `init-db`, `probe`, `find-start`, `sync`, `verify`, `verify-balances`, `inspect`, `balance`, `status`, `enrich`, `snapshot`, `discover`, `ccip`, `ccip-gaps`, `repair`, `reprocess`, `report`, `unified`. For `unified` it opens the one transaction, commits or rolls back, and writes the anomaly file. |
| `unified.py` | **The conductor of a unified run**: the seven steps, the alignment checks, what blocks and what is only an anomaly, the daily Top Holders rule, the lock against overlapping runs, the journal in `unified_runs`. |
| `sync.py` | Copies RIZE `Transfer` events chain by chain: first run from the deployment block, next runs from a few blocks before the checkpoint. One block range = one transaction, so re-running is harmless. |
| `logs.py` | Decodes `Transfer` logs and fetches a block range, splitting it automatically when a node refuses it or may have truncated the answer. |
| `rpc.py` | The JSON-RPC client: throttling, retries, batching, limit detection. Error messages never contain the URL (it contains the API key). |
| `config.py` | Reads `config/chains.json` and the environment (`DRPC_API_KEY`, `DATABASE_URL`, optional `RPC_URL_<CHAIN>`, `RPC_MAX_RPS`, `RPC_BATCH_SIZE`, `LOG_CAP`). |
| `db.py` | Everything about Postgres: connection, schema creation, the **staged connection** (all writes of a run stay invisible until `publish()`, `abort()` throws them away), advisory locks, writing a block range with its checkpoint, supply and balance helpers. |
| `enrich.py` | The market layer: turns a transaction receipt into a trade or liquidity rows, writes them (replacing what was stored for that transaction), keeps its checkpoint, photographs pools, discovers unknown pools, repairs crashed transactions, prints the `report`, and holds the lock helper used by corrections. |
| `receipts.py` | Reads one transaction like a bank statement: net gain or loss per address and per token, to find who really traded, which pools filled it, fees and relays, and the kind of move (buy, sell, arbitrage, liquidity, transfer). |
| `venues.py` | Adapters for the venues that are not « one pool = one contract »: Uniswap V4 and PancakeSwap Infinity (pool ids inside a manager contract), DODO private pools, RFQ market makers. |
| `registry.py` | Loads `labels/*.json` into memory: which addresses and pool ids are RIZE venues, entity names, tokens. Matches by address, never by symbol (impostor « RIZE » tokens exist). |
| `ccip.py` | Bridge reconciliation: asks the Chainlink CCIP API about every burn, stores the messages, and checks that what was burned on one chain was minted on the other. Also the backlog check used by the unified run. |
| `entities.py` | The Entities tab: groups addresses by the `entity` field of `labels/entities.json` (and every pool of a venue under the venue's name), adds up what they hold, ranks the entities. Written together with the Top Holders table. Team / tokenomics wallets (category `team`) are left out. |
| `tokens.py` | Names the « other assets »: every token seen in trades, liquidity moves and pools that nobody named is asked its `symbol()`, `name()` and `decimals()` through Multicall3 on the chain's node. Runs inside every unified run (never blocks it, only looks at what traded since the previous run) and as the command `tokens`, which catches up on the whole history (workflow **Explorize market layer**, command `tokens`). What `labels/tokens.json` says always wins. |
| `holders.py` | Builds the Top Holders ranking: live `balanceOf` on every chain + governance bonds on Base, written as one atomic snapshot. |
| `governance.py` | Reads the T-RIZE bonding contract on Base (every bond: owner, bonded, queued, vesting) through Multicall3, and checks it adds up. Also runnable alone as a read-only diagnostic. |
| `multicall.py` | Reads many contract values in one call through Multicall3 (one failing read never breaks the batch). |
| `reprocess.py` | The targeted correction behind **Explorize fix labels**: selects transactions (review queue, pool, address, flag, list of hashes), re-reads them, rewrites their rows, tells what changed. |
| `anomalies.py` | Writes `anomalies/ANOMALIES.txt`: each anomaly once, grouped by day, identified by a key so it is never repeated. |
| `checks.py` | The diagnostics behind `probe`, `verify`, `verify-balances`, `inspect`, `balance`, `status`. |
| `market_check.py` | The diagnostics `trade-check` and `classify` (read-only). |
| `__init__.py` | Marks the folder as a Python package. |

## Database, configuration, labels, corrections

| File | Folder | What it does in practice |
|---|---|---|
| `schema.sql` | `db/` | All the tables and indexes (listed above). Applied automatically and safely at every run (`CREATE … IF NOT EXISTS`). |
| `chains.json` | `config/` | Per chain: contract address, reorg margin, chunk sizes. Where you change a contract or a block-range setting. |
| `entities.json` | `labels/` | **The file you maintain by hand**: names and categories for addresses (exchanges, bridges, routers, relayers, teams…). A new line shows up on the site at the next run. |
| `pools.json` | `labels/` | The RIZE trading pools (address, or pool id for V4 / Infinity), their venue and tokens. A new pool here also needs **Explorize fix labels** to re-read its past transactions. |
| `venues.json` | `labels/` | Short venue names and logos (Aerodrome, Uniswap, PancakeSwap…). |
| `tokens.json` | `labels/` | Token symbols, decimals and stablecoins per chain, used to price trades in dollars. |
| `review_queries.sql` | `labels/` | The queries to paste in the Neon SQL editor to find what is unknown: unlabeled addresses, routers, unknown pools, guessed transactions, implausible prices. |
| `WORKFLOW.md` | `labels/` | The 4 working steps for labels and corrections, written out. |
| `transactions.txt` | `corrections/` | Optional list of special-case transactions to re-read (one per line); used when `use_file` is ticked in **Explorize fix labels**. |
| `ANOMALIES.txt` | `anomalies/` | Written by the unified workflow: everything noticed but not blocking, each item once, grouped by day. Read it from time to time. |
| `README.md` | `assets/explorize/` | The rules for logos: which folder, which file name, which size. |
| `.gitkeep` | `assets/explorize/venues/`, `entities/`, `tokens/`, `networks/`, `ui/` | Keeps the empty logo folders in Git. Logos you add there are served to the page by jsDelivr. |

## Tests (`tests/`)

Run with `TEST_DATABASE_URL=postgresql://… pytest` against a throwaway Postgres. Nothing here touches production.

| File | What it checks |
|---|---|
| `conftest.py` | Prepares a clean test database for each test. |
| `fakes.py` | A fake blockchain node with configurable limits, used by several tests. |
| `test_logs_and_rpc.py` | Log decoding, range splitting, throttling and retries, secrets never leaking. |
| `test_sync_db.py` | Syncing into the database: checkpoints, reorgs, idempotence, supply and balance rebuild. |
| `test_e2e_http.py` | The whole path command line → RPC client → fake node → Postgres. |
| `test_receipts.py` | Transaction reading: sells, buys, arbitrage, fees, relays, liquidity (modelled on real transactions). |
| `test_venues.py` | Uniswap V4, PancakeSwap Infinity, DODO and RFQ readings, with real amounts. |
| `test_enrich.py` | Receipt → stored rows, checkpointed job, repair, names written from the labels files. |
| `test_market_check.py` | The `trade-check` and `classify` diagnostics. |
| `test_ccip.py` | Bridge reconciliation against answers shaped like the real CCIP API. |
| `test_holders.py` | Top Holders: live balances plus bonds, atomic snapshot. |
| `test_entities.py` | Entities: grouping, venues, exclusions, written with the snapshot; automatic token names; addresses declared « not a pool ». |
| `test_labels_files.py` | The hand-edited labels files: valid addresses, known categories and venues, no duplicate label for one address. |
| `test_governance.py` | The governance diagnostic and Multicall3 helpers. |
| `test_unified.py` | The unified run: one transaction, alignment blocks, anomalies never block, the daily ranking rule. |
| `test_anomalies.py` | The anomaly file: each anomaly once, grouped by day. |
| `test_reprocess.py` | The targeted correction: selectors, dry run, rewrite after a label change, shared lock. |
| `__init__.py` | Marks the folder as a Python package. |

## The website side (repository TOKERIZE)

The page and its API live in the **TOKERIZE** repository; they only read the database this repository fills.

| File | Folder (TOKERIZE) | What it does in practice |
|---|---|---|
| `explorize.html` | `/` | The whole Explorize page: Trades / Liquidity / CCIP, Details (wallet and transaction search), Top Holders and Entities, Transfers, price chart; quick filters (funnel icon in the column headers). One file of HTML, CSS and JavaScript. |
| `vercel.json` | `/` | Routes `/explorize` to the page and `/api/explorize/*` to the endpoints below. |
| `status.js` | `api/explorize/` | Indexing state of every chain and the last successful unified update (« updated X min ago »). |
| `transfers.js` | `api/explorize/` | The Transfers table: raw, or one line per event (trade, liquidity move, bridge message, plain transfer, CEX IN / CEX OUT / CEX NTRAL), with filters (time, RIZE and USD range, from / to wallet or entity). The USD filter compares the exact value recorded for a trade and amount × current price for every other line. Also answers `?suggest=` for the search boxes of the quick filters. |
| `market.js` | `api/explorize/` | Trades, liquidity moves, pools and bridge messages for the market tabs. |
| `wallet.js` | `api/explorize/` | One wallet: balance per chain, governance bonds, label, and the daily balance history for the chart. |
| `tx.js` | `api/explorize/` | Every RIZE transfer of one transaction. |
| `holders.js` | `api/explorize/` | The Top Holders ranking, page by page. |
| `_party.js` | `api/explorize/` | « Who » filters shared by Transfers and Trades: a wallet or an entity (every address tagged with it, and the pools of a venue), plus the name search. |
| `_db.js`, `_lookups.js`, `_bonds.js` | `api/explorize/` | Shared helpers (read-only database access, name and token lookups, governance bonds). The leading underscore keeps Vercel from exposing them as endpoints. |
