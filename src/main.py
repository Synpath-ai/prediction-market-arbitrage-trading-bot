"""Entry point for the arbitrage bot.

    python -m src.main          # dry run: prices and decisions, no orders
    python -m src.main --live   # sends orders; needs both venues' credentials in .env
    python -m src.main --market <event id> kalshi:<TICKER>   # one market, instead of config.py
    python -m src.main --live --approve   # first live run: grant Polymarket's trading approvals
"""
from __future__ import annotations

import argparse
import asyncio
import signal
from contextlib import AsyncExitStack

from synpath import KalshiTrading, PolymarketTrading, load_credentials, require

from config import CONFIG

from .bot import ArbitrageBot


def markets_from(args: list[list[str]] | None) -> list[dict]:
    """`--market EVENT KALSHI_ID`, repeatable, in place of config.py's list."""
    return [{"event": e, "kalshi": k} for e, k in args] if args else CONFIG["markets"]


async def run(live: bool, markets: list[dict], approve: bool = False) -> None:
    if not markets:
        raise SystemExit("No markets configured. Find some with `python -m src.discover`, then add them to "
                         "config.py or pass --market EVENT_ID KALSHI_ID.")
    config = {**CONFIG, "markets": markets, "dry_run": CONFIG["dry_run"] and not live}
    async with AsyncExitStack() as stack:
        trading = {}
        if not config["dry_run"]:
            creds = load_credentials(dotenv=".env")
            trading["kalshi"] = await stack.enter_async_context(KalshiTrading(require("kalshi", creds)))
            poly = await stack.enter_async_context(PolymarketTrading(require("polymarket", creds)))
            await poly.ensure_api_credentials()          # derived from the wallet key; same every run
            if not all((await poly.check_approvals()).values()):
                if not approve:
                    raise SystemExit("Polymarket: this wallet has not approved trading yet. Run once with "
                                     "--live --approve (one wallet transaction), then start the bot normally.")
                print("[SETUP] granting Polymarket trading approvals (one wallet transaction)")
                await poly.approve_trading()
            trading["polymarket"] = poly
        bot = ArbitrageBot(config, trading=trading)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, bot.stop)
        await bot.start()
    print("\n[BOT STOPPED]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Kalshi x Polymarket cross-venue arbitrage bot")
    ap.add_argument("--live", action="store_true", help="send real orders (default: dry run)")
    ap.add_argument("--market", nargs=2, action="append", metavar=("EVENT_ID", "KALSHI_ID"),
                    help="a market to trade instead of config.py's list; repeatable")
    ap.add_argument("--approve", action="store_true",
                    help="with --live: grant Polymarket's one-time trading approvals if missing")
    a = ap.parse_args()
    asyncio.run(run(a.live, markets_from(a.market), a.approve))


if __name__ == "__main__":
    main()
