# Logos used by the Explorize page

Put one image per name. The file name is the `logo` value written in `labels/*.json` (without the extension),
or the chain key for `networks/`. The page tries `svg`, `png`, `jpg`, `jpeg`, `webp` in that order, so any of them works.

| Folder | What goes in it | Example |
|---|---|---|
| `venues/` | trading venues (DEX, RFQ) and what their pools show | `aerodrome.svg`, `uniswap.png`, `pancakeswap.png`, `dodo.svg` |
| `entities/` | everyone else: bridges, aggregators, liquidity managers, exchanges | `chainlink.svg`, `arrakis.png`, `kyberswap.png` |
| `tokens/` | token icons | `rize.svg`, `usdc.svg`, `eth.svg` |
| `networks/` | one round chain logo per network | `base.png`, `ethereum.png`, `bsc.png`, `polygon.png`, `canton.jpg` |
| `ui/` | small interface icons | `copy.png` (the copy icon shown when you hover an address) |

Notes:
- `networks/`: the BNB Chain logo can be called `bsc` or `bnb`. Canton Testnet reuses `canton` (or `canton-testnet` if you add one).
- A labelled wallet shows its round logo and its name, never the address. If an entity has a `logo` but the file is missing, the page shows its initials.
- Rules: square, 128x128 px or vector, under 20 KB, transparent background, lower-case file names without spaces.
- A missing file is not an error: the page shows the first letters of the name instead.
