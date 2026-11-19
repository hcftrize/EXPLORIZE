-- Explorize schema v1 (idempotent: safe to run many times)
-- Addresses and hashes are stored as bytea (20 / 32 bytes) to keep the
-- database small. Amounts are RAW token units (divide by 10^decimals to read).

CREATE TABLE IF NOT EXISTS chain_state (
  chain_id        integer PRIMARY KEY,
  slug            text        NOT NULL,
  token           bytea       NOT NULL,
  start_block     bigint,                 -- first block to scan (token deployment)
  last_block      bigint,                 -- last block fully indexed (checkpoint)
  last_synced_at  timestamptz,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS transfers (
  chain_id      integer       NOT NULL,
  block_number  bigint        NOT NULL,
  block_time    timestamptz   NOT NULL,
  tx_hash       bytea         NOT NULL,
  log_index     integer       NOT NULL,
  from_addr     bytea         NOT NULL,
  to_addr       bytea         NOT NULL,
  amount        numeric(78,0) NOT NULL,
  PRIMARY KEY (chain_id, tx_hash, log_index)
);

CREATE INDEX IF NOT EXISTS transfers_time_idx        ON transfers (block_time DESC);
CREATE INDEX IF NOT EXISTS transfers_chain_block_idx ON transfers (chain_id, block_number DESC);
CREATE INDEX IF NOT EXISTS transfers_chain_time_idx  ON transfers (chain_id, block_time DESC);
CREATE INDEX IF NOT EXISTS transfers_from_idx        ON transfers (from_addr, block_time DESC);
CREATE INDEX IF NOT EXISTS transfers_to_idx          ON transfers (to_addr, block_time DESC);

-- Filled by later modules (created now so the structure is stable) ----------

-- Wallet labels. chain_id = 0 means "valid on every chain".
CREATE TABLE IF NOT EXISTS labels (
  address   bytea   NOT NULL,
  chain_id  integer NOT NULL DEFAULT 0,
  label     text    NOT NULL,
  category  text    NOT NULL,   -- cex | dex | bridge | protocol | team | other
  source    text,
  PRIMARY KEY (address, chain_id)
);

-- Chainlink CCIP messages (cross-chain moves of RIZE).
CREATE TABLE IF NOT EXISTS ccip_messages (
  message_id       bytea       PRIMARY KEY,
  status           text,
  source_chain_id  integer,
  dest_chain_id    integer,
  source_tx        bytea,
  dest_tx          bytea,
  sender           bytea,
  receiver         bytea,
  amount           numeric(78,0),
  sent_at          timestamptz,
  received_at      timestamptz,
  raw              jsonb
);

CREATE INDEX IF NOT EXISTS ccip_source_tx_idx ON ccip_messages (source_chain_id, source_tx);
CREATE INDEX IF NOT EXISTS ccip_dest_tx_idx   ON ccip_messages (dest_chain_id, dest_tx);
CREATE INDEX IF NOT EXISTS ccip_sent_idx      ON ccip_messages (sent_at DESC);

-- Hourly USD price series (RIZE/USD), used for "USD at time of transaction".
CREATE TABLE IF NOT EXISTS prices_hourly (
  ts       timestamptz PRIMARY KEY,
  usd      numeric NOT NULL,
  source   text
);

-- Top Holders (rebuilt once a day by indexer/holders.py) ----------------------
-- Everything below is a snapshot read from the contracts; it is replaced as a whole at each run.

-- One row per bond that still holds RIZE (bonded or queued for release). Governance exists on Base only.
CREATE TABLE IF NOT EXISTS bonds (
  nft_id         integer       PRIMARY KEY,
  owner          bytea         NOT NULL,
  bonded         numeric(78,0) NOT NULL,   -- active bonded amount (includes the part still locked by vesting)
  queued         numeric(78,0) NOT NULL,   -- queued for release (brokenBond), still inside the contract
  vested_locked  numeric(78,0) NOT NULL,   -- part of `bonded` still locked by the vesting schedule
  release_queue  jsonb         NOT NULL DEFAULT '[]',  -- [{"amount": "<raw>", "time": <unix>}] (7 day unbonding)
  vesting        jsonb,                    -- {"amount","cliff","vesting","start","initiator"} when vested > 0
  updated_at     timestamptz   NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS bonds_owner_idx ON bonds (owner);

-- The ranking (top 1000). held = all chains; total = held + bonded + queued.
CREATE TABLE IF NOT EXISTS holders (
  rank           integer       PRIMARY KEY,
  address        bytea         NOT NULL UNIQUE,
  held           numeric(78,0) NOT NULL,
  held_by_chain  jsonb         NOT NULL DEFAULT '{}',
  bonded         numeric(78,0) NOT NULL,
  queued         numeric(78,0) NOT NULL,
  vested_locked  numeric(78,0) NOT NULL,
  bond_count     integer       NOT NULL,
  total          numeric(78,0) NOT NULL,
  updated_at     timestamptz   NOT NULL DEFAULT now()
);

-- One row: when the snapshot was taken and whether the checks passed.
CREATE TABLE IF NOT EXISTS holders_meta (
  id                smallint      PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  updated_at        timestamptz   NOT NULL,
  base_block        bigint,
  snapshot_time     timestamptz,
  holders           integer,
  addresses_read    integer,
  bonds_active      integer,
  contract_balance  numeric(78,0),
  bonded_total      numeric(78,0),
  queued_total      numeric(78,0),
  invariant_diff    numeric(78,0),   -- contract balance - (bonded + queued); 0 = everything adds up
  duration_s        real
);

-- Market layer: derived from `transfers` + receipts + the registry, always rebuildable ------------------------

-- RIZE trading pools. Mirror of labels/pools.json (the owner's file wins) plus the pools the code discovered
-- itself ("POOL UNKNW n" until the owner names them). pool_key = pool address (20 bytes) or pool id (32 bytes).
CREATE TABLE IF NOT EXISTS pools (
  chain_id      integer NOT NULL,
  pool_key      bytea   NOT NULL,
  name          text    NOT NULL,
  venue         text    NOT NULL DEFAULT '',
  kind          text    NOT NULL,                 -- v3 | v2 | v4 | infinity | rfq
  token0        bytea,
  token1        bytea,
  fee           integer,
  tick_spacing  integer,
  factory       bytea,
  gauge         bytea,
  manager       bytea,
  holder        bytea,                            -- address that holds the tokens when it is not the key
  status        text    NOT NULL DEFAULT 'active',
  source        text    NOT NULL DEFAULT 'manual',
  note          text,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chain_id, pool_key)
);

-- One row per transaction that is a trade (sell | buy | arbitrage), proven by Swap / fill events.
CREATE TABLE IF NOT EXISTS trades (
  chain_id     integer       NOT NULL,
  tx_hash      bytea         NOT NULL,
  block_number bigint        NOT NULL,
  block_time   timestamptz   NOT NULL,
  side         text          NOT NULL,            -- sell | buy | arbitrage
  trader       bytea,
  sender       bytea,                             -- who signed the transaction
  router       bytea,                             -- what the transaction called
  rize         numeric(78,0) NOT NULL,            -- RIZE on the pool side, exact (18 decimals, raw)
  counter      jsonb         NOT NULL DEFAULT '{}',   -- {"<token hex>": "<raw>"}; the zero address = native coin
  usd          numeric(24,6),                     -- dollars on the fills whose counter asset is a stablecoin
  price_usd    numeric(30,12),                    -- usd / rize over those fills
  fills        jsonb         NOT NULL DEFAULT '[]',   -- [{"pool","rize","counter","dir","usd"}]
  swap_count   integer       NOT NULL DEFAULT 0,
  tags         text[]        NOT NULL DEFAULT '{}',
  flags        text[]        NOT NULL DEFAULT '{}',   -- worth a look: events and transfers disagree, unknown pool id...
  extra        jsonb         NOT NULL DEFAULT '{}',   -- fees, profit, native legs, RIZE skimmed
  PRIMARY KEY (chain_id, tx_hash)
);
CREATE INDEX IF NOT EXISTS trades_time_idx   ON trades (block_time DESC);
CREATE INDEX IF NOT EXISTS trades_chain_time ON trades (chain_id, block_time DESC);
CREATE INDEX IF NOT EXISTS trades_trader_idx ON trades (trader, block_time DESC);

-- Liquidity moves: add | remove | collect, one row per pool event (singleton pools: per ModifyLiquidity).
CREATE TABLE IF NOT EXISTS liquidity_events (
  chain_id     integer       NOT NULL,
  tx_hash      bytea         NOT NULL,
  seq          integer       NOT NULL,
  block_number bigint        NOT NULL,
  block_time   timestamptz   NOT NULL,
  pool_key     bytea         NOT NULL,
  kind         text          NOT NULL,            -- add | remove | collect
  provider     bytea,
  rize         numeric(78,0),                     -- raw RIZE: + entered the pool, - left it (NULL = not readable)
  counter      jsonb         NOT NULL DEFAULT '{}',
  detail       jsonb         NOT NULL DEFAULT '{}',   -- ticks, salt, liquidity delta, tx verdict (rebalance...)
  PRIMARY KEY (chain_id, tx_hash, seq)
);
CREATE INDEX IF NOT EXISTS liquidity_time_idx ON liquidity_events (block_time DESC);
CREATE INDEX IF NOT EXISTS liquidity_pool_idx ON liquidity_events (chain_id, pool_key, block_time DESC);

-- What each address pool holds, photographed by the snapshot command (one row per pool and hour).
CREATE TABLE IF NOT EXISTS pool_snapshots (
  chain_id      integer       NOT NULL,
  pool_key      bytea         NOT NULL,
  ts            timestamptz   NOT NULL,
  block_number  bigint,
  rize          numeric(78,0) NOT NULL,
  counter_token bytea,
  counter       numeric(78,0),
  PRIMARY KEY (chain_id, pool_key, ts)
);

-- Where the enrichment of each chain stands.
CREATE TABLE IF NOT EXISTS enrich_state (
  chain_id    integer PRIMARY KEY,
  last_block  bigint,
  txs_done    bigint  NOT NULL DEFAULT 0,
  updated_at  timestamptz NOT NULL DEFAULT now()
);

-- Transactions the enrichment could not turn into a proven trade or liquidity move (guessed venue, crash, flag).
CREATE TABLE IF NOT EXISTS enrich_review (
  chain_id     integer     NOT NULL,
  tx_hash      bytea       NOT NULL,
  block_number bigint      NOT NULL,
  reason       text        NOT NULL,              -- guess | crash | flag | unknown_pool
  verdict      text,
  rize         numeric(78,0),
  detail       text,
  seen_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chain_id, tx_hash)
);

-- Burn transactions already looked up in the Chainlink CCIP API (so a burn with no CCIP message is not asked again every run).
CREATE TABLE IF NOT EXISTS ccip_checked (
  chain_id    integer     NOT NULL,
  tx_hash     bytea       NOT NULL,
  found       boolean     NOT NULL,
  checked_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (chain_id, tx_hash)
);

-- Names the site shows, written from the labels folder by the enrich command.
ALTER TABLE labels ADD COLUMN IF NOT EXISTS logo text;
CREATE TABLE IF NOT EXISTS venues (
  key   text PRIMARY KEY,
  name  text NOT NULL,
  kind  text NOT NULL DEFAULT 'dex',
  logo  text
);
CREATE TABLE IF NOT EXISTS tokens (
  chain_id  integer NOT NULL,
  address   bytea   NOT NULL,                     -- the zero address = the chain's native coin
  symbol    text    NOT NULL,
  decimals  integer NOT NULL,
  PRIMARY KEY (chain_id, address)
);

-- The site connects with a read-only role; give it the new tables (no-op if the role does not exist).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'explorize_ro') THEN
    GRANT SELECT ON bonds, holders, holders_meta, pools, trades, liquidity_events, pool_snapshots, enrich_state, enrich_review, ccip_messages, ccip_checked, venues, tokens TO explorize_ro;
  END IF;
END
$$;
