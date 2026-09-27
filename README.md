# Prediction Market Arbitrage Trading Bot

[![Powered by Synpath](https://img.shields.io/badge/Powered%20by-Synpath-7c3aed?style=for-the-badge)](https://www.synpath.dev)
![License: MIT](https://img.shields.io/badge/License-MIT-blue?style=for-the-badge)
![Paper trading only](https://img.shields.io/badge/Paper%20trading-no%20real%20orders-orange?style=for-the-badge)

An open-source arbitrage strategy for **Kalshi** and **Polymarket**: it finds arbitrage opportunities, runs the strategy on live prices as paper trading, and backtests it on history. No orders are placed.

**Powered by [Synpath](https://www.synpath.dev)**, one API for prediction markets. Synpath matches the same market across Kalshi and Polymarket and supplies the live order books, fee schedules and price history this bot runs on.

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

*Backtest of the strategy on three markets listed on both platforms: 60 days of hourly prices, 1,000 contracts per leg. **+$863.80 total P&L, a +29.4% return** on the $2,933 peak capital tied up at once, across 20 trades, every one closed at a profit. Produced with `python -m backtest.backtest` and `python -m backtest.charts --generic-names`.*

## How Cross-Platform Arbitrage Works

> **Find the same event priced differently → buy both sides for less than $1 → exit when profitable, or hold to settlement.**

Polymarket and Kalshi sometimes price the same event differently. Every contract pays **$1** if it's right and **$0** if not, so holding **YES on one platform and NO on the other** always pays exactly **$1** at settlement. If both sides together cost less than $1, the difference is profit.

### Example: 1,000 contracts

**Step 1: Buy both sides for less than $1**

| | Price |
|---|---|
| YES on Kalshi | 39¢ |
| NO on Polymarket | 55¢ |
| Fees | 2.7¢ |
| **Total cost** | **96.7¢** |
| Pays at settlement | $1.00 |
| **Profit locked in** | **3.3¢ per pair ≈ $33** |

**Step 2: Sell early if the prices converge**

If the gap closes before settlement and both sides can be sold for a combined **$1.02**, the bot sells.

**Profit: 2.6¢ per pair after fees ≈ $26**, and the capital is free for the next opportunity.

**Step 3: Otherwise, hold to settlement**

If the prices don't converge, the bot keeps the position. At settlement it collects the **3.3¢ per pair (≈ $33)** locked in at entry.

The bot automates all three steps across Polymarket and Kalshi.

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

### 3. Choose markets

Find markets listed on both platforms (see [Finding Arbitrage Opportunities](#finding-arbitrage-opportunities)), then add them to `config.py`:

```python
"markets": [
    {"event": "<synpath event id>", "kalshi": "kalshi:<TICKER>"},
],
```

### 4. Set the strategy

Adjust the rest of `config.py` to taste:

| Option | Description | Default |
|---|---|---|
| `entry_edge` | Minimum profit per $1 pair, after fees, to enter | `0.02` |
| `take_profit` | Minimum round-trip profit per pair to exit | `0.02` |
| `contracts` | Contracts per leg (capped by available liquidity) | `1000` |
| `require_same_rules` | Only trade markets whose rules match | `True` |
| `poll_interval_seconds` | Seconds between price checks | `60` |

## Finding Arbitrage Opportunities

The bot doesn't ship with any markets preselected. Use the discovery tool to list markets that trade on both platforms:

```bash
python -m src.discover                        # most traded events
python -m src.discover --query "senate"       # search by keyword
python -m src.discover --domain election --quotes
```

Market matching is done by Synpath, not by comparing titles. Each matched market comes with a rule check:

- **`same`**: both platforms settle the market the same way. These are the only markets used by default.
- **`insufficient`**: one platform's rules don't state every detail.
- **`not_same`**: the markets can settle differently, so it is **not** an arbitrage. Never used.

The discovery tool prints entries you can paste straight into `config.py`.

## Usage

```bash
python -m src.main                                      # every market in config.py
python -m src.main --market <event_id> kalshi:<TICKER>  # a single market
```

Every poll, the bot reads both platforms' live order books, prices both combinations, and prints what the strategy does: an **entry** signal opens a paper position at the quoted prices, an **exit** signal closes it at the bids and records the profit, and otherwise it reports the position being held. Paper positions are saved to `state/positions.json`, so they carry over after a restart, and every completed trade is recorded in `state/trades.csv`.

## Backtesting

```bash
python -m backtest.backtest --days 60
python -m backtest.charts
python -m backtest.charts --market kalshi:<TICKER> --start YYYY-MM-DD --end YYYY-MM-DD
```

The backtest runs the same strategy code on hourly price history and reports the trades and P&L for each market. The charts show, for each market:

- **Prices:** both platforms' prices, with the spread shaded and every entry and exit marked
- **P&L:** cumulative P&L, with the profit of each trade

Options: `--by-close` counts trades by the date they closed, `--best` shows each market's best run of consecutive trades, and `--generic-names` labels markets as Market A, B, C for sharing.

## Project Layout

```
prediction-market-arbitrage-trading-bot/
├── config.py            # Markets, strategy parameters, position size
├── src/
│   ├── main.py          # Entry point
│   ├── bot.py           # Live strategy loop, paper positions and trades
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

## Powered by Synpath

[![Synpath](https://img.shields.io/badge/Synpath-synpath.dev-7c3aed?style=for-the-badge)](https://www.synpath.dev)

This bot is built on **[Synpath](https://www.synpath.dev)**, one API for every prediction market. Synpath provides everything the strategy needs, for both Kalshi and Polymarket:

- **Market matching:** finds the same market on both platforms and checks that they settle under the same rules
- **Live order books:** best bid and ask, with size, on both platforms
- **Fee schedules:** each platform's real trading fees, so every profit figure is after fees
- **Price history:** the data behind the backtest

Get an API key at **[synpath.dev](https://www.synpath.dev)**.

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
