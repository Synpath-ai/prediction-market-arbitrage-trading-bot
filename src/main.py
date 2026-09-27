"""Entry point: run the strategy on live prices, paper trading (no orders are sent).

    python -m src.main
    python -m src.main --market <event id> kalshi:<TICKER>   # one market, instead of config.py
"""
from __future__ import annotations

import argparse
import asyncio
import signal

from config import CONFIG, configured_markets

from .bot import ArbitrageBot


def markets_from(args: list[list[str]] | None) -> list[dict]:
    """`--market EVENT KALSHI_ID`, repeatable, in place of config.py's list."""
    return [{"event": e, "kalshi": k} for e, k in args] if args else configured_markets()


async def run(markets: list[dict]) -> None:
    if not markets:
        raise SystemExit("No markets configured. Run `python -m src.discover --save` first, "
                         "or pass --market EVENT_ID KALSHI_ID.")
    bot = ArbitrageBot({**CONFIG, "markets": markets})
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, bot.stop)
    await bot.start()
    print("\n[STOPPED]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Kalshi x Polymarket arbitrage strategy, paper trading on live prices")
    ap.add_argument("--market", nargs=2, action="append", metavar=("EVENT_ID", "KALSHI_ID"),
                    help="a market to run instead of config.py's list; repeatable")
    asyncio.run(run(markets_from(ap.parse_args().market)))


if __name__ == "__main__":
    main()
