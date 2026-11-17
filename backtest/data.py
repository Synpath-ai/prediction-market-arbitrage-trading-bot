"""Price history for a pair, read through synpath, one bar per minute by default.

  Kalshi       the venue's candles, with the closing YES bid and ask. Kalshi only returns a
               minute in which something changed; quieter minutes carry the last price forward.
  Polymarket   no historical book is published, so one price per bar from either
                 source="quotes"  the venue's sampled displayed price (every minute covered)
                 source="trades"  bars built from executions (a minute without trades has none)
               used as both bid and ask. A real Polymarket spread would cost about half of it per leg.

`fetch_ohlcv` splits long windows into the requests each venue accepts. Sizes are not in the
candles, so the backtest assumes the configured size fills at the quoted price.
"""
from __future__ import annotations

import synpath
from math import isfinite

from src.arbitrage import Book
from src.matcher import Pair

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS
DAY_MS = 24 * HOUR_MS
TIMEFRAMES = {"1m": MINUTE_MS, "5m": 5 * MINUTE_MS, "1h": HOUR_MS}
"""Bar sizes the backtest runs on. Both venues answer all three."""


def _bar(ms: int, step: int) -> int:
    return ms - ms % step


def kalshi_history(market_id: str, since: int, until: int, *, timeframe: str = "1m") -> dict[int, Book]:
    step = TIMEFRAMES[timeframe]
    out: dict[int, Book] = {}
    for c in synpath.Kalshi().fetch_ohlcv(market_id, timeframe=timeframe, since=since, until=until):
        bid = c.bid_close
        ask = c.ask_close
        if bid is not None or ask is not None:
            out[_bar(c.timestamp, step)] = Book(bid, ask)
    return out


def poly_history(
    market_id: str,
    since: int,
    until: int,
    *,
    source: str = "quotes",
    flipped: bool = False,
    timeframe: str = "1m",
    spread: float = 0.02,
) -> dict[int, Book]:
    """Build estimated bid/ask books around historical reference prices.

    `spread` is the assumed full bid/ask spread, not a measured historical spread.
    """
    try:
        valid_spread = (
            not isinstance(spread, bool)
            and isfinite(spread)
            and 0 <= spread <= 1
        )
    except (TypeError, ValueError, OverflowError):
        valid_spread = False

    if not valid_spread:
        raise ValueError(
            "spread must be a finite number between 0 and 1"
        )

    step = TIMEFRAMES[timeframe]
    half_spread = spread / 2
    out: dict[int, Book] = {}

    for c in synpath.Polymarket().fetch_ohlcv(
        market_id,
        timeframe=timeframe,
        since=since,
        until=until,
        source=source,
    ):
        if c.close is not None:
            bid = max(0.0, c.close - half_spread)
            ask = min(1.0, c.close + half_spread)
            book = Book(bid, ask)
            out[_bar(c.timestamp, step)] = book.flipped() if flipped else book

    return out


def history(
    pair: Pair,
    since: int,
    until: int,
    *,
    source: str = "quotes",
    timeframe: str = "1m",
    poly_spread: float = 0.02,
) -> list[tuple[int, Book, Book]]:
    """Combine synchronized quotes, rejecting missing and stale observations."""
    step = TIMEFRAMES[timeframe]
    max_kalshi_age = 2 * step

    kalshi = kalshi_history(
        pair.kalshi_id, since, until, timeframe=timeframe
    )
    poly = poly_history(
        pair.poly_id,
        since,
        until,
        source=source,
        flipped=pair.flipped,
        timeframe=timeframe,
        spread=poly_spread,
    )

    rows = []
    last_kalshi = None
    last_kalshi_time = None

    for t in sorted(set(kalshi) | set(poly)):
        if t in kalshi:
            last_kalshi = kalshi[t]
            last_kalshi_time = t

        # Polymarket must have an observation at this exact timestamp.
        current_poly = poly.get(t)

        if last_kalshi is None or current_poly is None:
            continue

        # Do not reuse Kalshi quotes beyond the freshness limit.
        if t - last_kalshi_time > max_kalshi_age:
            continue

        rows.append((t, last_kalshi, current_poly))

    return rows
