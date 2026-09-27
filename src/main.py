"""Entry point for the arbitrage bot.

    python -m src.main          # dry run: prices and decisions, no orders
    python -m src.main --live   # sends orders; needs both venues' credentials in .env
"""
from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import AsyncExitStack

from synpath import KalshiTrading, PolymarketTrading, load_credentials, require

from config import CONFIG

from .bot import ArbitrageBot


async def run(live: bool) -> None:
    config = {**CONFIG, "dry_run": CONFIG["dry_run"] and not live}
    async with AsyncExitStack() as stack:
        trading = {}
        if not config["dry_run"]:
            creds = load_credentials(dotenv=".env")
            trading["kalshi"] = await stack.enter_async_context(KalshiTrading(require("kalshi", creds)))
            trading["polymarket"] = await stack.enter_async_context(PolymarketTrading(require("polymarket", creds)))
        bot = ArbitrageBot(config, trading=trading)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.stop)
        await bot.start()
    print("\n[BOT STOPPED]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Kalshi x Polymarket cross-venue arbitrage bot")
    ap.add_argument("--live", action="store_true", help="send real orders (default: dry run)")
    asyncio.run(run(ap.parse_args().live))


if __name__ == "__main__":
    main()
