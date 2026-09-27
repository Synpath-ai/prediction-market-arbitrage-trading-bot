"""Configuration for the arbitrage bot and the backtest.

Every market is a Kalshi listing plus the Synpath catalog event it belongs to. The bot asks
Synpath's matching service for the Polymarket listing of the same proposition, and refuses a
pair whose two venues' rules are not compared as the same (see src/matcher.py).
"""

CONFIG = {
    # Markets to trade: the Kalshi listing and its event on https://www.synpath.dev/events/<event>.
    "markets": [
        {"event": "284999d5-9859-4789-8f16-a8998ff05551", "kalshi": "kalshi:KXGOVAK-26-JKRE"},   # Alaska Governor
        {"event": "737088c1-d043-4ae8-bf24-d0194bb4818a", "kalshi": "kalshi:HOUSETX15-26-R"},    # TX-15 House
        {"event": "71143731-5474-4aec-b144-f783dadc39cc", "kalshi": "kalshi:GOVPARTYTX-26-R"},   # Texas Governor
    ],

    # Strategy, in dollars per $1 pair (0.02 = 2c).
    # Enter when buying YES on one venue and NO on the other locks at least this after taker fees.
    "entry_edge": 0.02,
    # Sell both legs only when the round trip nets at least this after fees on both sides.
    # Otherwise hold: the pair pays exactly $1 at resolution, so it keeps the edge locked at entry.
    "take_profit": 0.02,

    # Size: contracts per leg, the same number of YES and NO so the pair is hedged. The bot also
    # never takes more than the size quoted at the best price on either leg.
    "contracts": 1000,

    # Only trade pairs whose rules the catalog compares as the same (`same`). Setting this to
    # False also accepts `insufficient`; a `not_same` pair is never traded.
    "require_same_rules": True,

    # Seconds between checks.
    "poll_interval_seconds": 60,

    # No orders are sent in a dry run: fills are assumed at the quoted prices and logged.
    "dry_run": True,

    # Where the bot keeps its open positions and its closed trades between runs.
    "state_file": "state/positions.json",
    "trades_file": "state/trades.csv",
}
