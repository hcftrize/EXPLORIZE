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

-- The site connects with a read-only role; give it the new tables (no-op if the role does not exist).
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'explorize_ro') THEN
    GRANT SELECT ON bonds, holders, holders_meta TO explorize_ro;
  END IF;
END
$$;
