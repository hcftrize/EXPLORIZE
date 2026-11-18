# Logos used by the Explorize page

Put one image per name, file name = the `logo` value written in `labels/*.json`.

| Folder | What goes in it | Example |
|---|---|---|
| `venues/` | trading venues (DEX, RFQ) | `aerodrome.svg`, `uniswap.svg`, `pancakeswap.svg` |
| `entities/` | everyone else: bridges, aggregators, liquidity managers, exchanges | `chainlink.svg`, `arrakis.svg`, `kyberswap.svg` |
| `tokens/` | token icons | `rize.svg`, `usdc.svg`, `eth.svg` |

Rules: SVG or PNG, square, 128x128 px or vector, under 20 KB, transparent background, lower-case file names without spaces.
A missing file is not an error: the page shows the first letters of the name instead.
