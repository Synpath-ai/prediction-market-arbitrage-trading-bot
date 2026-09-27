"""Configuration for the arbitrage bot and the backtest.

Every market is a Kalshi listing plus the Synpath catalog event it belongs to. The bot asks
Synpath's matching service for the Polymarket listing of the same proposition, and refuses a
pair whose two venues' rules are not compared as the same (see src/matcher.py).
"""

CONFIG = {
    # Markets to trade. Each is a Kalshi listing and the Synpath catalog event it belongs to
    # (https://www.synpath.dev/events/<event>). `python -m src.discover` lists candidates and
    # prints entries in this shape, ready to paste:
    #   {"event": "<catalog event id>", "kalshi": "kalshi:<TICKER>"},
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

    # Seconds between checks.
    "poll_interval_seconds": 60,

    # Where the paper positions and closed trades are kept between runs.
    "state_file": "state/positions.json",
    "trades_file": "state/trades.csv",
}
