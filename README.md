# Super Market Trading Bot

An automated Python bot for **The Super Market** 2026 Midterm Elections tournament
(SIG site). It targets a **safe top-N finish**: locked-profit arbitrage first,
directional trades only where you have an independent view, and hard caps so no
single settlement can sink your standing.

## Strategy

Each cycle executes in this priority order:

1. **Take profit (`close`).** If you hold a side with a fair-value view and the
   market has reached that value, sell into the book. Closes bypass all caps.
2. **Arbitrage (multi-outcome markets).** At most one outcome can win, so buying
   NO on every outcome pays at least `n - 1`. When the best YES bids sum above 1,
   that basket locks in `sum(bids) - 1` per set. It is placed atomically via
   `/orders/multi-leg`. A YES basket (`sum(asks) < 1`) is opt-in because it is
   only safe when exactly one outcome must win. Binary markets have no internal
   arbitrage, since YES ask + NO ask = 1 + spread.
   **Across linked markets**, the platform's relationship graph (`/relationships`)
   says which markets are logically tied. The bot checks those links against live
   bid/ask, not the engine's last-traded prices, and only trades a basket that
   pays at least 1 per set whatever happens:
   - *implication* (A ⇒ B, e.g. "wins by 10+" ⇒ "wins"): if bid(A) > ask(B), buy
     YES B and NO A;
   - *complementary* (exactly one true): YES basket if asks sum < 1, NO basket if
     bids sum > 1;
   - *mutually exclusive*: NO basket if bids sum > 1; YES basket only when marked
     exhaustive;
   - *monotonic* thresholds: treated as implications, oriented by the engine's own
     suggested corrective trades.

   Boolean and conditional relationships are listed but never auto-traded. Two
   baskets that share an exchange are never placed in the same cycle.
3. **Mispricing (`open`).** When your fair value beats the executable price by
   `MIN_EDGE`, the bot walks the book while the edge holds and buys up to a
   fractional-Kelly target. Existing holdings count toward that target.

Risk caps are fractions of **equity** (cash + position value): per market, total,
a cash reserve, and a net D-vs-R cap across exchanges you tag with a `lean`.
An open is skipped if fair value and the book differ by more than `MAX_EDGE`
(0.25). A gap that large usually means a bad mapping.

Fair values come from **Kalshi and Polymarket** prices for exchanges you approve
(see below), optionally overridden by your own `fair_values.json`. With neither,
the bot runs **arbitrage-only**.

## Order hygiene

- Prices are snapped to the API's 0.005 tick. Buys round down and sells round up.
- Every order is a limit order with a TTL (`ORDER_TTL_SECONDS`).
- Live cycles start by cancelling the bot's open orders in the tournament, so
  remainders never accumulate. This also cancels orders placed by hand.
- Live cycles do nothing outside the tournament window (from `startDate`/`endDate`).
- Retries reuse the same `idempotencyKey`, so a retried order is never placed twice.

## Layout

```
src/supermarket_bot/
  config.py      settings (SUPERMARKET_* env vars)
  models.py      API models, tick snapping, trading window
  client.py      REST client (auth, retries, pagination, tournament scoping)
  external.py    read-only Kalshi / Polymarket price feeds
  mapping.py     mappings.json + title matching for `suggest`
  fair_value.py  fair-value providers (external / static JSON / composite)
  signals.py     close / arbitrage / mispricing detection
  relationships.py  cross-market arbitrage from the relationship graph
  risk.py        equity-based sizing and caps
  execution.py   multi-leg + batch submission (dry-run aware)
  engine.py      one cycle + run loop
  __main__.py    CLI: tournaments | status | markets | suggest | check | once | run
bot              launcher script (use ./bot ...)
```

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt && pip install -e .
cp .env.example .env   # set SUPERMARKET_API_KEY; keep DRY_RUN=true at first
```

The key needs the `read` and `trade` scopes. Accept the current terms on the site
first, or orders fail with `TERMS_NOT_ACKNOWLEDGED`.

## Usage

Run commands with `./bot` from the project folder. It works even when macOS
breaks the `supermarket-bot` command (see Troubleshooting).

```bash
./bot tournaments      # find your tournament slug -> SUPERMARKET_TOURNAMENT_SLUG
./bot status           # window, cash, rank, positions
./bot markets          # market + exchange ids
./bot suggest          # propose Kalshi/Polymarket matches into mappings.json
./bot approve          # bulk-approve clean matches and fill D/R lean (review after!)
./bot check            # approved mappings: Super Market book vs outside fair value
./bot relationships    # linked markets + any cross-market arbitrage right now (-v: list all)
./bot once             # one dry-run cycle
./bot run              # loop (add --fair-value fair_values.json for your own views)
```

To go live, read the `[DRY-RUN]` lines from `once`. When they look right, set
`SUPERMARKET_DRY_RUN=false`.

## Outside prices (Kalshi + Polymarket)

Both sites publish market data for free with no account. The bot only reads it
and never trades there. One batched request per site per cycle stays far below
their published rate limits.

1. `./bot suggest` searches both sites for each open Super Market exchange. It
   writes `mappings.json` with the top 3 candidates per site and pre-fills the
   best one. Every entry starts as `"approved": false`.
2. Review each entry in `mappings.json`:
   - Check `ref` points at the same question, with the same race, year and
     resolution. Swap in another candidate from `suggestions` if needed, or set
     the source to `null`.
   - Set `"invert": true` if Super Market YES is that contract's NO.
   - Add `"lean": "D"` or `"R"` (the party a YES outcome favours) for the
     correlation cap.
   - Set `"approved": true`. Only approved entries are traded.
3. `./bot check` shows the Super Market bid/ask next to the outside fair value,
   so you can spot bad matches before trading. A fair value near `1 - price`
   usually means a missing `invert`.

Each cycle, per approved exchange, the bot:

- ignores quotes with a spread wider than `EXTERNAL_MAX_SPREAD` (0.08);
- skips the exchange if Kalshi and Polymarket mids differ by more than
  `EXTERNAL_MAX_DISAGREEMENT` (0.05);
- uses the volume-weighted midpoint as fair value, with confidence 0.8 for two
  agreeing sources or 0.5 for one, lowered as spreads widen;
- gets no fair value from a source that fails to respond. Stale prices are
  never reused.

Re-running `suggest` adds new markets and never changes approved entries.

## Your own views (optional)

`fair_values.json` maps `exchangeId` to your probability that YES resolves
true, plus optional `confidence` and `lean`. See `fair_values.example.json`. It
overrides outside prices for any exchange it lists. The file is re-read every cycle.
If it becomes unreadable, the last good values are kept.

## Market making (optional, off by default)

With `SUPERMARKET_MAKER_ENABLED=true`, each cycle the bot also rests two limit
orders on up to `MAKER_MAX_MARKETS` exchanges that have a fair value:

- a YES bid at about `fair - MAKER_HALF_SPREAD`;
- a YES ask at about `fair + MAKER_HALF_SPREAD`, sent as a NO buy at `1 - ask`.

If both fill, the YES and NO shares cancel into 1 SUSQie per pair, so the round
trip earns the gap whichever way the race goes. Quotes are cancelled and
replaced every cycle.

Pricing: never at or through fair value, at most one tick ahead of the current
best, and never crossing the book, so quotes always rest instead of taking.

Inventory control:
- quotes shift away from held inventory (`MAKER_SKEW`), so the reducing side
  gets more attractive;
- at `MAKER_MAX_INVENTORY` net cost, the side that would add stops quoting;
- new inventory also respects `MAKER_BUDGET`, the cash reserve, the per-race
  cap and the D-vs-R cap.

The two strategies never mix. The maker records the exchanges it manages in
`state/maker.json` and stays out of races where buy-and-hold holds a position.
Buy-and-hold stays out of the maker's exchanges. `./bot status` tags maker
positions `MM`. Maker inventory counts against `MAKER_BUDGET`, not
`MAX_TOTAL_EXPOSURE`.

The maker stops quoting `MAKER_STOP_HOURS_BEFORE_END` hours (default 36) before
the tournament ends. On election night prices jump faster than a 60-second
cycle can requote. Inventory left over is held to settlement. Turning the
maker off hands that inventory back to buy-and-hold.

## Development

```bash
ruff check src tests && mypy && pytest
```

## Troubleshooting

`ModuleNotFoundError: No module named 'supermarket_bot'` from `supermarket-bot`
means macOS set the `hidden` flag on the editable-install `.pth` file, and
Python 3.13 skips hidden `.pth` files. It keeps coming back for projects on the
Desktop. Use `./bot` instead, which doesn't depend on that file. For a one-off
fix, run `chflags nohidden .venv/lib/python3.13/site-packages/*.pth`.

## Limitations

- The Super Market orderbook endpoint is slow and uneven (sub-second to 20s+).
  The bot fetches books concurrently (`SCAN_CONCURRENCY`) with a per-request
  timeout, so a full ~240-market scan takes seconds and one stalled market is
  skipped rather than hanging the cycle.
- The bot takes liquidity at visible prices. It does not quote inside the spread.
- Cross-market baskets count against the total-exposure cap and cash reserve,
  not the per-market cap, since their payoff is locked.
- An arbitrage basket is accepted atomically, but fills are not guaranteed. If a
  leg rests, it is cancelled and a warning is logged, and that basket may be
  partly unhedged.
- If positions cannot be valued, the API returns 409, so that cycle is skipped
  rather than trading blind.
