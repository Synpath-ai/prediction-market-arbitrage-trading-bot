"""Configuration for the arbitrage bot and the backtest.

Every market is a Kalshi listing plus the Synpath catalog event it belongs to. The bot asks
Synpath's matching service for the Polymarket listing of the same proposition, and refuses a
pair whose two venues' rules are not compared as the same (see src/matcher.py).
"""
import json
from pathlib import Path

CONFIG = {
    # Markets to run. Left empty, the markets saved by `python -m src.discover --save`
    # (markets.json) are used. Or list them here:
    #   {"event": "<synpath event id>", "kalshi": "kalshi:<TICKER>"},
    "markets": [],

    # Strategy, in dollars per $1 pair (0.02 = 2c).
    # Enter when buying YES on one venue and NO on the other locks at least this after taker fees.
    "entry_edge": 0.02,
    # Sell both legs only when the round trip nets at least this after fees on both sides.
    # Otherwise hold: the pair pays exactly $1 at resolution, so it keeps the edge locked at entry.
    "take_profit": 0.02,

    # Size: contracts per leg, the same number of YES and NO so the pair is hedged, never more than
    # the size quoted at the best price on either leg.
    "contracts": 1000,

    # Only trade pairs whose rules the catalog compares as the same (`same`). Setting this to
    # False also accepts `insufficient`; a `not_same` pair is never traded.
    "require_same_rules": True,

    # Live prices come over the venues' WebSockets. Kalshi's needs a Kalshi API key (KALSHI_KEY_ID
    # and KALSHI_PRIVATE_KEY_PATH in .env); without one, Kalshi's book is read every this many seconds.
    "kalshi_poll_seconds": 2,
    # At most one status line per market this often; entries and exits are always printed.
    "status_interval_seconds": 60,

    # Where the paper positions and closed trades are kept between runs.
    "state_file": "state/positions.json",
    "trades_file": "state/trades.csv",
}


MARKETS_FILE = Path(__file__).parent / "markets.json"


def configured_markets() -> list[dict]:
    """config.py's markets, or else the ones `python -m src.discover --save` wrote."""
    if CONFIG["markets"]:
        return CONFIG["markets"]
    if MARKETS_FILE.exists():
        return [{"event": m["event"], "kalshi": m["kalshi"]} for m in json.loads(MARKETS_FILE.read_text())]
    return []
