"""Hourly history for a pair, read through synpath.

  Kalshi       the venue's candles, with the closing YES bid and ask.
  Polymarket   no historical book is published, so one price per hour from either
                 source="quotes"  the venue's sampled displayed price (every hour covered)
                 source="trades"  bars built from executions (an hour without trades has none)
               used as both bid and ask. A real Polymarket spread would cost about half of it per leg.

`fetch_ohlcv` splits long windows into the requests each venue accepts. Sizes are not in the
candles, so the backtest assumes the configured size fills at the quoted price.
"""
from __future__ import annotations

import synpath

from src.arbitrage import Book
from src.matcher import Pair

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


def _hour(ms: int) -> int:
    return ms - ms % HOUR_MS


def kalshi_history(market_id: str, since: int, until: int) -> dict[int, Book]:
    out: dict[int, Book] = {}
    for c in synpath.Kalshi().fetch_ohlcv(market_id, timeframe="1h", since=since, until=until):
        bid = c.bid_close if c.bid_close is not None else c.close
        ask = c.ask_close if c.ask_close is not None else c.close
        if bid is not None or ask is not None:
            out[_hour(c.timestamp)] = Book(bid, ask)
    return out


def poly_history(market_id: str, since: int, until: int, *, source: str = "quotes", flipped: bool = False) -> dict[int, Book]:
    out: dict[int, Book] = {}
    for c in synpath.Polymarket().fetch_ohlcv(market_id, timeframe="1h", since=since, until=until, source=source):
        if c.close is not None:
            book = Book(c.close, c.close)
            out[_hour(c.timestamp)] = book.flipped() if flipped else book
    return out


def history(pair: Pair, since: int, until: int, *, source: str = "quotes") -> list[tuple[int, Book, Book]]:
    """One row per hour from the first hour both venues have a price, each side carried forward
    through hours where its venue printed nothing."""
    kalshi = kalshi_history(pair.kalshi_id, since, until)
    poly = poly_history(pair.poly_id, since, until, source=source, flipped=pair.flipped)
    rows, k, p = [], None, None
    for h in sorted(set(kalshi) | set(poly)):
        k, p = kalshi.get(h, k), poly.get(h, p)
        if k is not None and p is not None:
            rows.append((h, k, p))
    return rows
