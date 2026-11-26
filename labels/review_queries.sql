-- EXPLORIZE: review queries for the Neon SQL editor (read-only). Run them one at a time.
-- Addresses come out as 0x... text; chain_id: 8453 Base, 1 Ethereum, 56 BSC, 137 Polygon.
-- Typical use: find what is unknown (A-F), label it in labels/*.json, then fix the past with the "Explorize fix labels" workflow.

-- A1. Addresses that move the most RIZE, with no label (last 90 days; shorten the interval if the editor times out)
SELECT '0x' || encode(x.a, 'hex') AS address, x.chain_id, count(*) AS transfers, round(sum(x.amount) / 1e18) AS rize_moved
  FROM (SELECT chain_id, from_addr AS a, amount FROM transfers WHERE block_time > now() - interval '90 days'
        UNION ALL
        SELECT chain_id, to_addr, amount FROM transfers WHERE block_time > now() - interval '90 days') x
 WHERE x.a <> '\x0000000000000000000000000000000000000000'
   AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.address = x.a AND l.chain_id IN (0, x.chain_id))
   AND NOT EXISTS (SELECT 1 FROM pools p WHERE p.pool_key = x.a AND p.chain_id = x.chain_id)
 GROUP BY x.a, x.chain_id ORDER BY rize_moved DESC LIMIT 100;

-- A2. The biggest holders today with no label (balances read on chain at the last published block)
SELECT '0x' || encode(b.addr, 'hex') AS address, b.chain_id, round(b.balance / 1e18) AS rize_held
  FROM holder_balances b
 WHERE b.balance > 0
   AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.address = b.addr AND l.chain_id IN (0, b.chain_id))
   AND NOT EXISTS (SELECT 1 FROM pools p WHERE p.pool_key = b.addr AND p.chain_id = b.chain_id)
 ORDER BY b.balance DESC LIMIT 100;

-- B1. Routers, relayers and signers of trades with no label
SELECT '0x' || encode(x.a, 'hex') AS address, x.chain_id, x.role, count(*) AS trades, round(sum(x.rize) / 1e18) AS rize
  FROM (SELECT chain_id, router AS a, 'router' AS role, rize FROM trades WHERE router IS NOT NULL
        UNION ALL SELECT chain_id, sender, 'sender', rize FROM trades WHERE sender IS NOT NULL AND sender <> trader) x
 WHERE NOT EXISTS (SELECT 1 FROM labels l WHERE l.address = x.a AND l.chain_id IN (0, x.chain_id))
 GROUP BY x.a, x.chain_id, x.role ORDER BY rize DESC LIMIT 100;

-- B2. Traders with no label, by RIZE traded (the big ones: market makers, bots, funds)
SELECT '0x' || encode(t.trader, 'hex') AS address, t.chain_id, count(*) AS trades, round(sum(t.rize) / 1e18) AS rize,
       round(sum(coalesce(t.usd, 0))) AS usd
  FROM trades t
 WHERE t.trader IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.address = t.trader AND l.chain_id IN (0, t.chain_id))
 GROUP BY t.trader, t.chain_id ORDER BY rize DESC LIMIT 100;

-- B3. Liquidity providers with no label
SELECT '0x' || encode(e.provider, 'hex') AS address, e.chain_id, count(*) AS moves, round(sum(abs(e.rize)) / 1e18) AS rize
  FROM liquidity_events e
 WHERE e.provider IS NOT NULL
   AND NOT EXISTS (SELECT 1 FROM labels l WHERE l.address = e.provider AND l.chain_id IN (0, e.chain_id))
 GROUP BY e.provider, e.chain_id ORDER BY rize DESC NULLS LAST LIMIT 100;

-- C1. What waits for review, by reason (guess | flag | unknown_pool | crash)
SELECT chain_id, reason, verdict, count(*) FROM enrich_review GROUP BY 1, 2, 3 ORDER BY 4 DESC;

-- C2. The same, by explanation (to see patterns)
SELECT chain_id, reason, left(detail, 110) AS detail, count(*) FROM enrich_review GROUP BY 1, 2, 3 ORDER BY 4 DESC LIMIT 40;

-- D1. Unknown pools: the identifier to put in labels/pools.json, and how many transactions wait on it
--     (the 18 first characters are enough for the --pool field of the fix workflow)
SELECT chain_id, substring(detail from 'unknown_pool_id:[0-9a-fx]+') AS pool, count(*) AS transactions, round(sum(rize) / 1e18) AS rize
  FROM enrich_review
 WHERE reason = 'unknown_pool' OR detail LIKE '%unknown_pool_id%'
 GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 50;

-- D2. Pools found by the code itself and still unnamed (POOL UNKNW n): name them in labels/pools.json
SELECT p.chain_id, '0x' || encode(p.pool_key, 'hex') AS pool, p.name, p.venue, p.kind, p.updated_at
  FROM pools p WHERE p.source = 'auto' OR p.name LIKE 'POOL UNKNW%' ORDER BY p.chain_id, p.updated_at;

-- E1. Transactions whose venue had to be guessed, newest first
SELECT chain_id, '0x' || encode(tx_hash, 'hex') AS tx, block_number, reason, verdict, left(detail, 90) AS detail, seen_at
  FROM enrich_review WHERE reason = 'guess' ORDER BY seen_at DESC LIMIT 100;

-- E2. Crashed transactions (the next unified run retries them by itself)
SELECT chain_id, '0x' || encode(tx_hash, 'hex') AS tx, left(detail, 120) AS detail, seen_at
  FROM enrich_review WHERE reason = 'crash' ORDER BY seen_at DESC LIMIT 50;

-- F1. Trades with an implausible price
SELECT chain_id, '0x' || encode(tx_hash, 'hex') AS tx, block_time, side, rize / 1e18 AS rize, usd, price_usd, flags
  FROM trades WHERE 'price-implausible' = ANY(flags) ORDER BY block_time DESC LIMIT 100;

-- F2. Trades by flag (how many of each)
SELECT chain_id, unnest(flags) AS flag, count(*) FROM trades GROUP BY 1, 2 ORDER BY 3 DESC;

-- G. One transaction in detail: what we stored about it (replace the hash; 0x is fine)
SELECT 'trade' AS what, side, '0x' || encode(trader, 'hex') AS trader, '0x' || encode(router, 'hex') AS router, rize / 1e18 AS rize, usd, flags::text AS note
  FROM trades WHERE tx_hash = decode(substr('0xPUT_THE_HASH_HERE', 3), 'hex')
UNION ALL
SELECT 'review', verdict, reason, NULL, rize / 1e18, NULL, detail
  FROM enrich_review WHERE tx_hash = decode(substr('0xPUT_THE_HASH_HERE', 3), 'hex')
UNION ALL
SELECT 'liquidity', kind, '0x' || encode(pool_key, 'hex'), '0x' || encode(provider, 'hex'), rize / 1e18, NULL, NULL
  FROM liquidity_events WHERE tx_hash = decode(substr('0xPUT_THE_HASH_HERE', 3), 'hex');


-- 15. Tokens named automatically from their contract ("other assets"): the ones that still need a logo.
--     Add assets/explorize/tokens/<logo>.svg (or png / jpg / webp); the file name is the lower-case symbol suggested below.
--     source = auto: read from the contract. unreadable: the contract did not answer like a token (not asked again; the `tokens`
--     command with "rebuild" ticked asks again). To force a name, write the token in labels/tokens.json (the file always wins).
WITH used AS (
  SELECT chain_id, k AS token, count(*) AS uses FROM trades, LATERAL jsonb_object_keys(counter) AS k GROUP BY 1, 2
  UNION ALL
  SELECT chain_id, k, count(*) FROM liquidity_events, LATERAL jsonb_object_keys(counter) AS k GROUP BY 1, 2)
SELECT t.chain_id, '0x' || encode(t.address, 'hex') AS token, t.symbol, t.name, t.decimals, t.source,
       lower(regexp_replace(t.symbol, '[^A-Za-z0-9]+', '-', 'g')) AS logo_file_name,
       coalesce(sum(u.uses), 0) AS uses_in_trades_and_liquidity
  FROM tokens t LEFT JOIN used u ON u.chain_id = t.chain_id AND lower(u.token) = encode(t.address, 'hex')
 WHERE t.source IN ('auto', 'unreadable')
 GROUP BY t.chain_id, t.address, t.symbol, t.name, t.decimals, t.source
 ORDER BY uses_in_trades_and_liquidity DESC, t.chain_id;


-- 16. Liquidity managers (proxies, vaults, bots): who moves liquidity, how many of their transactions ALSO swap (these are the ones
--     a rebalance can fool), and how many are still to correct (to_correct > 0 = run "Explorize fix labels" with lp_txs).
WITH todo AS (          -- the transactions the current reading can change and an older reading stored (what lp_txs selects)
  SELECT l.chain_id, l.tx_hash FROM liquidity_events l GROUP BY l.chain_id, l.tx_hash
  HAVING bool_or(NOT (l.detail ? 'rv'))
     AND (bool_or(l.kind = 'collect') OR (bool_or(l.kind = 'add') AND bool_or(l.kind = 'remove'))
          OR EXISTS (SELECT 1 FROM trades t WHERE t.chain_id = l.chain_id AND t.tx_hash = l.tx_hash)))
SELECT l.chain_id, '0x' || encode(l.provider, 'hex') AS provider, coalesce(lb.label, '') AS label,
       count(DISTINCT l.tx_hash) AS lp_txs,
       count(DISTINCT l.tx_hash) FILTER (WHERE EXISTS (SELECT 1 FROM trades t WHERE t.chain_id = l.chain_id AND t.tx_hash = l.tx_hash)) AS also_swap,
       count(DISTINCT l.tx_hash) FILTER (WHERE EXISTS (SELECT 1 FROM todo d WHERE d.chain_id = l.chain_id AND d.tx_hash = l.tx_hash)) AS to_correct,
       min(l.block_time)::date AS first_seen, max(l.block_time)::date AS last_seen
  FROM liquidity_events l
  LEFT JOIN LATERAL (SELECT label FROM labels WHERE address = l.provider AND chain_id IN (0, l.chain_id) ORDER BY chain_id DESC LIMIT 1) lb ON true
 GROUP BY 1, 2, 3
 ORDER BY also_swap DESC, lp_txs DESC
 LIMIT 30;

-- 17. What is still to correct (0 everywhere = the whole history has been read by the current code). The first line is exactly
--     what "Explorize fix labels" with lp_txs selects; the second is what outliers selects (trades priced far from their day).
WITH todo AS (
  SELECT l.chain_id, l.tx_hash FROM liquidity_events l GROUP BY l.chain_id, l.tx_hash
  HAVING bool_or(NOT (l.detail ? 'rv'))
     AND (bool_or(l.kind = 'collect') OR (bool_or(l.kind = 'add') AND bool_or(l.kind = 'remove'))
          OR EXISTS (SELECT 1 FROM trades t WHERE t.chain_id = l.chain_id AND t.tx_hash = l.tx_hash))),
day AS (
  SELECT chain_id, date_trunc('day', block_time) AS d, percentile_cont(0.5) WITHIN GROUP (ORDER BY price_usd) AS med
    FROM trades WHERE price_usd IS NOT NULL GROUP BY 1, 2)
SELECT chain_id, 'lp_txs: liquidity transactions to correct' AS what, count(*) AS n FROM todo GROUP BY 1
UNION ALL
SELECT t.chain_id, 'outliers: trades far from their day, not re-read', count(*)
  FROM trades t JOIN day ON day.chain_id = t.chain_id AND day.d = date_trunc('day', t.block_time)
 WHERE t.price_usd IS NOT NULL AND NOT (t.extra ? 'rv') AND day.med > 0 AND (t.price_usd > 3 * day.med OR t.price_usd < day.med / 3)
 GROUP BY 1
ORDER BY 1, 2;

-- 18. Trades more than 3x above or below the median price of their chain that day (a wrong reading leaves these behind).
--     read_by_new_code = true: the current code looked at it again and kept it (a real odd trade); false: not re-read yet
--     (run "Explorize fix labels" with outliers ticked).
WITH day AS (
  SELECT chain_id, date_trunc('day', block_time) AS d, percentile_cont(0.5) WITHIN GROUP (ORDER BY price_usd) AS med
    FROM trades WHERE price_usd IS NOT NULL GROUP BY 1, 2)
SELECT t.chain_id, '0x' || encode(t.tx_hash, 'hex') AS tx, t.block_time, t.side, t.rize / 1e18 AS rize, t.usd, t.price_usd,
       round(day.med::numeric, 6) AS median_of_the_day, (t.extra ? 'rv') AS read_by_new_code
  FROM trades t JOIN day ON day.chain_id = t.chain_id AND day.d = date_trunc('day', t.block_time)
 WHERE t.price_usd IS NOT NULL AND day.med > 0 AND (t.price_usd > 3 * day.med OR t.price_usd < day.med / 3)
 ORDER BY t.block_time DESC
 LIMIT 100;
