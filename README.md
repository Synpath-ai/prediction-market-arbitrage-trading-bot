# Prediction Market Arbitrage Trading Bot

An open-source bot that finds and trades arbitrage opportunities between **Kalshi** and **Polymarket**, with a backtester for the same strategy. Built on [Synpath](https://www.synpath.dev), a unified API for prediction markets.

> **Disclaimer:** Not financial advice. For research and educational purposes only.

---

## Overview

Kalshi and Polymarket often list the same question at different prices. When that happens, buying **YES on one platform** and **NO on the other** can cost less than the $1 the pair always pays out. That difference is the arbitrage profit.

```
Profit per pair = $1.00 − (YES price + NO price) − trading fees
```

This bot:

- **Matches markets** across both platforms and checks that they settle under the same rules
- **Detects arbitrage opportunities** from live order books, after fees
- **Executes both legs** at the same time, in equal size
- **Exits the position** once the spread converges and the profit target is reached
- **Backtests** the same strategy on historical prices and charts the results

## Arbitrage Strategy

The bot trades the spread between the two platforms rather than waiting for the market to resolve.

| Step | Condition | Action |
|---|---|---|
| **Entry** | YES + NO + fees ≤ $1 − `entry_edge` | Buy YES on the cheaper platform and NO on the other |
| **Exit** | Selling both legs nets ≥ `take_profit` after fees | Sell both legs and realize the profit |
| **Hold** | Neither condition is met | Keep the position, since at resolution it still pays $1 per pair |

Because the position is only closed at a profit, and otherwise held until resolution, **it never sells at a loss**. The profit locked in at entry is the worst case.

**Execution details:**
- Both legs are sent together as **immediate-or-cancel limit orders** at the quoted prices, so no order rests on the book and nothing fills worse than quoted.
- YES and NO are always bought in the **same number of contracts**, so the position stays hedged.
- If one leg fills more than the other, the excess is sold back immediately.
- Order size is capped by the liquidity at the best price on both platforms.

## Installation

Requires Python 3.10+.

```bash
git clone https://github.com/Synpath-ai/prediction-market-arbitrage-trading-bot
cd prediction-market-arbitrage-trading-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

For live trading, copy the credentials template and fill in your keys:

```bash
cp .env.example .env
```

Orders are signed and sent from your machine directly to Kalshi and Polymarket. Your keys are never shared.

## Finding Arbitrage Opportunities

The bot doesn't ship with any markets preselected. Use the discovery tool to list markets that trade on both platforms:

```bash
python -m src.discover                        # most traded events
python -m src.discover --query "senate"       # search by keyword
python -m src.discover --domain election --quotes
```

Market matching is done by Synpath, not by comparing titles. Each matched market comes with a rule check:

- **`same`**: both platforms settle the market the same way. These are the only markets traded by default.
- **`insufficient`**: one platform's rules don't state every detail.
- **`not_same`**: the markets can settle differently, so it is **not** an arbitrage. Never traded.

The discovery tool prints entries you can paste straight into `config.py`.

## Usage

```bash
python -m src.main                                      # dry run (no orders)
python -m src.main --market <event_id> kalshi:<TICKER>  # trade a single market
python -m src.main --live                               # live trading
```

In dry run mode, the bot reads live prices and logs every decision without sending orders. Open positions are saved to `state/positions.json`, so the bot resumes them after a restart, and every completed trade is recorded in `state/trades.csv`.

## Backtesting

```bash
python -m backtest.backtest --days 60
python -m backtest.charts
python -m backtest.charts --market kalshi:<TICKER> --start YYYY-MM-DD --end YYYY-MM-DD
```

The backtest runs the same strategy code on hourly price history and reports the trades and P&L for each market. The charts show, for each market:

- **Prices:** both platforms' prices, with the spread shaded and every entry and exit marked
- **P&L:** cumulative P&L, with the profit of each trade

Options: `--by-close` counts trades by the date they closed, and `--best` shows each market's best run of consecutive trades.

## Configuration

Settings live in `config.py`:

| Option | Description | Default |
|---|---|---|
| `markets` | Markets to trade (use `src.discover` to find them) | `[]` |
| `entry_edge` | Minimum profit per $1 pair, after fees, to enter | `0.02` |
| `take_profit` | Minimum round-trip profit per pair to exit | `0.02` |
| `contracts` | Contracts per leg (capped by available liquidity) | `1000` |
| `require_same_rules` | Only trade markets whose rules match | `True` |
| `poll_interval_seconds` | Seconds between price checks | `60` |
| `dry_run` | Log decisions without placing orders | `True` |

## Project Layout

```
prediction-market-arbitrage-trading-bot/
├── config.py            # Markets, strategy parameters, position size
├── src/
│   ├── main.py          # Entry point
│   ├── bot.py           # Trading loop, order execution, position state
│   ├── strategy.py      # Entry and exit rules
│   ├── arbitrage.py     # Arbitrage pricing, fees, profit calculation
│   ├── matcher.py       # Cross-platform market matching and rule check
│   └── discover.py      # Finds markets listed on both platforms
├── backtest/
│   ├── data.py          # Historical prices
│   ├── backtest.py      # Strategy simulation
│   └── charts.py        # Entry/exit and P&L charts
└── tests/               # Offline unit tests
```

## Risks & Limitations

- **Execution risk:** the two legs fill on different platforms, and prices can move between them.
- **Liquidity:** many markets have thin order books; large orders will not fill at the best price.
- **Capital lock-up:** a position that never reaches the profit target is held until resolution.
- **Settlement risk:** the arbitrage only holds if both platforms resolve the market the same way. Always read both platforms' rules.
- **Backtest assumptions:** hourly data, Polymarket's historical price used as both bid and ask, and fills assumed at the quoted price.

Start with a small position size and verify the bot's behavior before scaling up.

## Powered By

- [Synpath](https://www.synpath.dev): unified API for Kalshi and Polymarket (market matching, order books, fees, history, order entry)
- Python

## License

MIT
