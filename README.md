# Cross-Venue Spread Trader

Trades the price gap between Kalshi and Polymarket when both list the same market, and backtests the same rule on history. It runs on [Synpath](https://www.synpath.dev), which supplies the market pairing, live order books, fee schedules, price history and order entry for both venues.

You choose the markets. The repo contains the rule, the plumbing and the tools to find and test markets; it does not ship with any preselected.

> Research and educational code. Not investment advice. You are responsible for anything it trades.

---

## The rule

A YES contract on one venue plus a NO contract on the other, for the same question, pays exactly $1 at resolution whichever way it goes. When the two venues disagree on the price, that pair can be bought for less than $1.

- **Open** a pair when, after both venues' taker fees, it costs at least `entry_edge` less than $1. That margin is locked in from the moment both legs fill.
- **Close** it early, selling both legs at the bids, as soon as the round trip nets at least `take_profit` after fees on both sides.
- **Otherwise keep it.** The pair still pays $1 at resolution, so the locked margin is the floor. The bot never sells a pair at a loss.

Both legs are always the same number of contracts, so the position stays hedged, and the size never exceeds what the book shows at the best price on either leg. Each market holds at most one open pair at a time.

## Getting set up

Requires Python 3.10+.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Synpath key; venue keys only if you trade live
```

Venue credentials stay on your machine: orders go directly from the bot to Kalshi and Polymarket.

## Picking markets

List Kalshi markets that Polymarket also lists, with how the two venues' rules compare:

```bash
python -m src.discover                          # most traded events
python -m src.discover --query "governor"       # search
python -m src.discover --domain election --quotes
```

Each line shows a rule verdict:

| Verdict | Meaning |
|---|---|
| `same` | Every settlement condition Synpath checks agrees. Traded by default. |
| `insufficient` | A condition could not be confirmed from one venue's rules. |
| `not_same` | The venues can settle differently, so YES + NO is not guaranteed to pay $1. Never traded. |

The command ends with `config.py` entries for the `same` pairs. Paste the ones you want into `markets`, or pass them on the command line with `--market EVENT_ID KALSHI_ID`. Read both venues' rules yourself before trading real money.

## Running the bot

```bash
python -m src.main                              # dry run: live books, decisions logged, no orders
python -m src.main --market <event id> kalshi:<TICKER>
python -m src.main --live                       # places orders
```

Each poll reads both books, prices both combinations (YES on Kalshi + NO on Polymarket, and the reverse), and acts on the rule above. Orders are immediate-or-cancel limits at the quoted prices, so nothing rests on the book and nothing fills worse than quoted. If one leg fills more than the other, the difference is sold straight back, leaving only matched pairs.

Open pairs are written to `state/positions.json` and picked up again after a restart. Every closed round trip is appended to `state/trades.csv`.

## Backtesting

```bash
python -m backtest.backtest --days 60
python -m backtest.charts
python -m backtest.charts --market kalshi:<TICKER> --start YYYY-MM-DD --end YYYY-MM-DD
```

The backtest replays hourly history through the same pricing and rule code the bot uses and prints each market's trades and P&L at your configured size. `charts` draws two views per market:
- **Prices:** both venues' prices with the spread shaded, and each entry and exit marked.
- **P&L:** the running total, with one bar per round trip.

Add `--by-close` to count trades by the date they closed, or `--best` to show each market's strongest run of consecutive trades.

History has limits worth knowing:
- Kalshi candles carry the closing bid and ask each hour.
- Polymarket publishes no historical book, so its hourly price stands in for both bid and ask, which is slightly optimistic.
- Fills are assumed at the quoted price for the full size.

Treat backtest results as a rough guide, not a forecast.

## Tuning

All settings live in `config.py`.

- **`entry_edge`** (default `0.02`): minimum locked margin per $1 pair to open. Lower means more trades, each thinner.
- **`take_profit`** (default `0.02`): round-trip profit per pair that triggers an early close. Higher means fewer early exits and longer holds.
- **`contracts`** (default `1000`): contracts per leg, capped by the depth at the best price.
- **`require_same_rules`** (default `True`): trade only `same` pairs. `False` also admits `insufficient`.
- **`poll_interval_seconds`** (default `60`): how often the bot checks prices.
- **`dry_run`** (default `True`): log decisions without sending orders. `--live` overrides it.

## Code map

| Path | Role |
|---|---|
| `src/arbitrage.py` | Prices both combinations from two books: fees, locked margin, round-trip value |
| `src/strategy.py` | The open/close rule, shared by the bot and the backtest |
| `src/matcher.py` | Resolves the Polymarket side of a Kalshi market and its rule verdict |
| `src/discover.py` | Lists candidate markets |
| `src/bot.py`, `src/main.py` | Live loop, order placement, saved state |
| `backtest/` | History loader, simulator, charts |
| `tests/` | Offline tests; no network or keys needed (`pytest`) |

## Risks to understand before going live

- **Leg risk:** the two legs fill on different venues. One can fill partly or not at all while the other fills.
- **Capital lock-up:** a pair that never reaches `take_profit` is held until the market resolves, possibly months away.
- **Settlement mismatch:** the $1 floor depends on both venues resolving the question the same way.
- **Depth:** books on many markets are thin, and large sizes will not fill at the top of the book.

Start with a small `contracts` value and watch a few round trips before scaling up.

## License

MIT
