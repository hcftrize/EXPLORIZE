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

## Workflows (Actions tab, all run manually or via the GitHub API)

| Workflow | What it does |
|---|---|
| **Explorize probe** | Checks key, contracts, real `eth_getLogs` limits, archive support and the database. Tick `init_db` the first time to create the tables. |
| **Explorize backfill** | One-off full history of the 4 chains in parallel. Resumable: run it again if it stops. Needs an archive node (dRPC Pay As You Go). |
| **Explorize sync** | Incremental update (default: all chains). Meant to be triggered hourly. |
| **Explorize verify** | Supply integrity check and per-chain status. |

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
