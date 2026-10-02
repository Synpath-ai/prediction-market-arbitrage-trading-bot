# Prediction Market Arbitrage Trading Bot

[![Powered by Synpath](https://img.shields.io/badge/Powered%20by-Synpath-7c3aed?style=for-the-badge)](https://www.synpath.dev)
[![Synpath on GitHub](https://img.shields.io/badge/GitHub-Synpath--ai%2Fsynpath-181717?style=for-the-badge&logo=github)](https://github.com/Synpath-ai/synpath)
![License: MIT](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)

An open-source arbitrage strategy for **Kalshi** and **Polymarket**: it finds arbitrage opportunities, runs the strategy on live prices as paper trading, and backtests it on history. No orders are placed.

Powered by [Synpath](https://www.synpath.dev): one API for every prediction market. Kalshi, Polymarket, Polymarket US and Opinion through one open-source Python SDK:

- **Smart order routing** across both order books, at the cheapest price after fees
- **Advanced order types:** stop-limit, trailing stop, OCO, TWAP and more, on every venue
- **Cross-venue market matching**, with settlement rules compared
- **Unified data:** order books, fees, live streams and history

Open source (MIT): **[github.com/Synpath-ai/synpath](https://github.com/Synpath-ai/synpath)** · `pip install synpath`

> [!WARNING]
> **Not financial advice. For research and educational purposes only.**
> This project does not place trades and makes no promise of profit. Backtest and paper-trading results are simulated and do not predict real returns. Read the full [Disclaimer](#disclaimer) before using it.

---

## Overview

Kalshi and Polymarket often list the same question at different prices. When that happens, buying **YES on one platform** and **NO on the other** can cost less than the $1 the pair always pays out. That difference is the arbitrage profit.

```
Profit per pair = $1.00 − (YES price + NO price) − trading fees
```

This bot:

- **Matches markets** across both platforms and checks that they settle under the same rules
- **Detects arbitrage opportunities** from live order books, after fees
- **Signals entries and exits** and tracks paper positions and P&L at the quoted prices
- **Backtests** the same strategy on historical prices and charts the results

![Backtest P&L across three markets](assets/backtest-pnl.png)

*Example backtest on three markets listed on both Kalshi and Polymarket: 60 days of 1-minute prices (Jul 29 – Sep 27, 2026), 1,000 contracts per leg. **+$1,771.40 total P&L, a +60.4% return** on the $2,934 peak capital tied up at once, across 44 trades, every one closed at a profit.*

## Backtest vs. live trading

| | Backtest | Live trading |
|---|---|---|
| Command | `python -m backtest.backtest` | `python -m trading` |
| Prices | historical 1-minute bars (`--timeframe 1h` for hourly) | live order books over the venues' WebSockets |
| Orders | simulated | **paper trading**: no orders are placed |
| Needs | a Synpath API key | a Synpath API key; a Kalshi API key for Kalshi's WebSocket (optional) |

**Placing real orders** is done with **[Synpath](https://github.com/Synpath-ai/synpath)**, the library this bot is built on: limit, IOC and FOK orders on Kalshi, Polymarket and Opinion, plus the live fill streams a two-legged trade needs. This bot stops at the signal.

## How Cross-Platform Arbitrage Works

Polymarket and Kalshi sometimes price the same event differently.

The bot looks for cases where it can buy **YES on one platform** and **NO on the other** for a combined cost of less than $1.

Because exactly one side will pay $1 at settlement, buying both sides below $1 creates a built-in profit.

### Simple example

Suppose:

- YES on Kalshi = **39¢**
- NO on Polymarket = **55¢**

Buying both costs **94¢**. After fees, the total cost is about **96.7¢**.

At settlement, one of the two positions will pay **$1**, so the trade locks in roughly **3.3¢ profit per contract pair**. On 1,000 contracts, that's about **$33**.

### The bot doesn't always need to wait

If the price difference disappears before the event settles, the bot can close both positions early and take the profit.

For example, if the two positions can later be sold for a combined **$1.02**, that's about **2.6¢ profit per pair after fees** (about $26 on 1,000 contracts). The bot exits and frees up the capital for the next opportunity.

If prices don't converge, it simply holds until settlement and collects the spread it locked in at entry.

### In short

**Find the same event priced differently → buy both sides for less than $1 → exit when profitable or hold to settlement.**

The bot automates this process across Polymarket and Kalshi.

## Arbitrage Strategy

Three simple rules:

1. **Buy** when YES on one platform plus NO on the other costs at least **2¢ less than $1**, after fees.
2. **Sell** both sides as soon as selling them makes at least **2¢ profit** per pair, after fees.
3. **Otherwise hold** until settlement, where the pair pays $1 and the profit locked in at entry is collected.

So the strategy **never sells at a loss**. YES and NO are always bought in the same quantity, so the position stays balanced, and the size is limited to what's available at the best price on each platform.

Both 2¢ thresholds can be changed in `config.py` (`entry_edge` and `take_profit`).

## Setup

### 1. Install

Requires Python 3.10+.

```bash
git clone https://github.com/Synpath-ai/prediction-market-arbitrage-trading-bot
cd prediction-market-arbitrage-trading-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Add your Synpath API key

```bash
cp .env.example .env        # then set SYNPATH_API_KEY
```

Get a key at [synpath.dev](https://www.synpath.dev). It's used for market matching, live prices and history. No exchange accounts or trading keys are needed.

### 3. Find markets

```bash
python -m src.discover --save
```

This finds the **10 markets with the best arbitrage profit available right now** (after fees), among markets listed on both Kalshi and Polymarket that settle under the same rules, and saves them to `markets.json`. The bot and the backtest use them automatically. It reads live prices for every candidate, so it can take a minute.

Change what it picks with `--limit 20`, `--sort gap` (largest price difference right now) or `--sort volume` (most traded), and narrow the search with `--query "senate"` or `--domain election`.

For live Kalshi prices over WebSocket, also add a Kalshi API key (`KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH`): Kalshi signs every WebSocket connection, even for market data. Without one, the bot reads Kalshi's public order book every 2 seconds instead. Polymarket's WebSocket needs no key.

### 4. Set the strategy

Adjust the rest of `config.py` to taste:

| Option | Description | Default |
|---|---|---|
| `entry_edge` | Minimum profit per $1 pair, after fees, to enter | `0.02` |
| `take_profit` | Minimum round-trip profit per pair to exit | `0.02` |
| `contracts` | Contracts per leg (capped by available liquidity) | `1000` |
| `require_same_rules` | Only trade markets whose rules match | `True` |
| `kalshi_poll_seconds` | Seconds between Kalshi book reads when there is no Kalshi key | `2` |
| `status_interval_seconds` | At most one status line per market this often (entries and exits always print) | `60` |

## Finding Arbitrage Opportunities

The bot doesn't ship with any markets preselected. Use the discovery tool to list markets that trade on both platforms:

```bash
python -m src.discover                        # 10 markets with the best profit right now
python -m src.discover --sort volume --limit 5  # most traded
python -m src.discover --query "senate" --save  # search by keyword and save
```

Market matching is done by Synpath, not by comparing titles. Each matched market comes with a rule check:

- **`same`**: both platforms settle the market the same way. These are the only markets used by default.
- **`insufficient`**: one platform's rules don't state every detail.
- **`not_same`**: the markets can settle differently, so it is **not** an arbitrage. Never used.

Add `--save` to keep the `same` markets in `markets.json`, which the bot and backtest then use. To pick markets by hand instead, list them in `config.py` or pass `--market EVENT_ID KALSHI_ID`.

## Live Trading

```bash
python -m trading                                      # every saved market
python -m trading --market <event_id> kalshi:<TICKER>  # a single market
```

The bot subscribes to both platforms' order books over WebSocket (Polymarket's always; Kalshi's when a Kalshi key is set, otherwise it reads Kalshi every 2 seconds) and re-prices a market the moment either book moves. It prints what the strategy does: an **entry** signal opens a paper position at the quoted prices, an **exit** signal closes it at the bids and records the profit, and otherwise it reports the position being held, at most once a minute per market. Paper positions are saved to `state/positions.json`, so they carry over after a restart, and every completed trade is recorded in `state/trades.csv`.

No orders are sent. To trade for real, place the two legs with [Synpath](https://github.com/Synpath-ai/synpath). Two exchanges can't fill one atomic trade, so one leg can fill while the other doesn't: the usual answer is to rest a limit order on the thinner side and, the moment it fills, take the other side with a fill-or-kill order, with an unwind ready if the hedge misses.

## Backtesting

```bash
python -m backtest.backtest --days 60                     # 1-minute bars
python -m backtest.backtest --days 60 --timeframe 1h      # hourly bars
python -m backtest.backtest --until "2026-09-27 11:00"    # a fixed window, to reproduce a run
python -m backtest.charts
python -m backtest.charts --market kalshi:<TICKER> --start YYYY-MM-DD --end YYYY-MM-DD
```

The backtest runs the same strategy code on 1-minute price history (hourly or 5-minute with `--timeframe`) and reports the trades and P&L for each market. The charts show, for each market:

- **Prices:** both platforms' prices, with the spread shaded and every entry and exit marked
- **P&L:** cumulative P&L, with the profit of each trade

Options: `--by-close` counts trades by the date they closed, `--best` shows each market's best run of consecutive trades, and `--generic-names` labels markets as Market A, B, C for sharing.

## Project Layout

```
prediction-market-arbitrage-trading-bot/
├── config.py            # Markets, strategy parameters, position size
├── trading/
│   ├── __main__.py      # Entry point: python -m trading
│   ├── bot.py           # Live strategy loop, paper positions and trades
│   └── feed.py          # Live order books over the venues' WebSockets
├── src/
│   ├── strategy.py      # Entry and exit rules
│   ├── arbitrage.py     # Arbitrage pricing, fees, profit calculation
│   ├── matcher.py       # Cross-platform market matching and rule check
│   └── discover.py      # Finds markets listed on both platforms
├── backtest/
│   ├── data.py          # Historical 1-minute (or hourly) prices
│   ├── backtest.py      # Strategy simulation
│   └── charts.py        # Entry/exit and P&L charts
└── tests/               # Offline unit tests
```

## Powered by Synpath

[![Synpath](https://img.shields.io/badge/Synpath-synpath.dev-7c3aed?style=for-the-badge)](https://www.synpath.dev)
[![Synpath on GitHub](https://img.shields.io/badge/GitHub-Synpath--ai%2Fsynpath-181717?style=for-the-badge&logo=github)](https://github.com/Synpath-ai/synpath)

This bot is built on **[Synpath](https://www.synpath.dev)**, one API for every prediction market. It uses Synpath's cross-venue market matching, live order books, fee schedules and price history.

Synpath does much more than this bot needs:

| | |
|---|---|
| **Venues** | Kalshi, Polymarket, Polymarket US and Opinion, through one SDK |
| **Smart order routing** | One order across Kalshi and Polymarket, filled from the cheapest price after fees |
| **Advanced order types** | Stop, stop-limit, trailing stop, iceberg, OCO, bracket, TWAP and peg on every venue |
| **Market matching** | The same market on every platform, with settlement rules compared |
| **Data** | Unified order books, trades, fees, live streams and tick-level Kalshi order book history |
| **Execution engine** | Orders journaled before sending, pre-trade risk rules, crash-safe restarts |

Get an API key at **[synpath.dev](https://www.synpath.dev)**. The Synpath SDK is open source (MIT) at **[github.com/Synpath-ai/synpath](https://github.com/Synpath-ai/synpath)**.

## Disclaimer

**Read this before using this project.**

- **Not financial advice.** Nothing in this repository is investment, financial, legal or tax advice, or a recommendation to buy or sell anything.
- **Educational and research use only.** The code is provided to demonstrate a strategy, not as a trading product.
- **No trading.** The bot only paper trades: it records simulated positions at quoted prices and never places real orders.
- **Simulated results.** Backtest and paper-trading results use historical or quoted prices and assume every order fills at those prices. Real trading involves slippage, partial fills, delays and liquidity limits, and results can be very different. Past performance does not predict future results.
- **No guarantee of profit.** "Locked-in" profit depends on both platforms settling the market the same way. Rules can differ, markets can be disputed or voided, and you can lose money.
- **Check the rules where you live.** Prediction markets are restricted or prohibited in some jurisdictions. You are responsible for complying with the laws that apply to you and with each platform's terms of service.
- **Not affiliated.** This project is not affiliated with, endorsed by, or sponsored by Kalshi or Polymarket.
- **Use at your own risk.** The software is provided "as is", without warranty of any kind (see the MIT license). The authors are not liable for any loss arising from its use.

## License

MIT
