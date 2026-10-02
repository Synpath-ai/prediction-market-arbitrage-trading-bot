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
        bid = c.bid_close if c.bid_close is not None else c.close
        ask = c.ask_close if c.ask_close is not None else c.close
        if bid is not None or ask is not None:
            out[_bar(c.timestamp, step)] = Book(bid, ask)
    return out


def poly_history(market_id: str, since: int, until: int, *, source: str = "quotes", flipped: bool = False,
                 timeframe: str = "1m") -> dict[int, Book]:
    step = TIMEFRAMES[timeframe]
    out: dict[int, Book] = {}
    for c in synpath.Polymarket().fetch_ohlcv(market_id, timeframe=timeframe, since=since, until=until, source=source):
        if c.close is not None:
            book = Book(c.close, c.close)
            out[_bar(c.timestamp, step)] = book.flipped() if flipped else book
    return out


def history(pair: Pair, since: int, until: int, *, source: str = "quotes",
            timeframe: str = "1m") -> list[tuple[int, Book, Book]]:
    """One row per bar from the first bar both venues have a price, each side carried forward
    through bars where its venue printed nothing."""
    kalshi = kalshi_history(pair.kalshi_id, since, until, timeframe=timeframe)
    poly = poly_history(pair.poly_id, since, until, source=source, flipped=pair.flipped, timeframe=timeframe)
    rows, k, p = [], None, None
    for t in sorted(set(kalshi) | set(poly)):
        k, p = kalshi.get(t, k), poly.get(t, p)
        if k is not None and p is not None:
            rows.append((t, k, p))
    return rows
