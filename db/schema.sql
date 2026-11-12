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
