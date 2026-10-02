"""Live top of book for every pair, from the venues' WebSockets.

  Polymarket   `PolymarketMarketStream`: public, no account needed.
  Kalshi       `KalshiStream` when Kalshi API credentials are configured (KALSHI_KEY_ID and
               KALSHI_PRIVATE_KEY_PATH): Kalshi signs every WebSocket connection, even for
               market data. Without them, Kalshi's public REST book is read every
               `kalshi_poll_seconds` instead, so the bot still runs with a Synpath key alone.

Each stream keeps a local book per market and repairs it after a gap or a reconnect; a book is
only read while it is marked ready. `changes()` yields the pair whose book just moved, which is
what the bot re-prices on.
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

import synpath

from src.arbitrage import Book
from src.matcher import Pair


def top_of(book: Any) -> Book | None:
    """A stream's local book as the top-of-book `Book` the strategy prices, or None while the
    book is not ready (before its snapshot, or after a gap until it is repaired)."""
    if book is None or not getattr(book, "ready", False):
        return None
    bid, ask = book.best_bid, book.best_ask
    return Book(
        float(bid) if bid is not None else None,
        float(ask) if ask is not None else None,
        float(book.bids[bid]) if bid is not None else None,
        float(book.asks[ask]) if ask is not None else None,
    )


def rest_top(client: Any, market_id: str) -> Book:
    b = client.fetch_order_book(market_id, side="yes", depth=1)
    bid, ask = b.best_bid, b.best_ask
    return Book(bid.price if bid else None, ask.price if ask else None,
                bid.size if bid else None, ask.size if ask else None)


class LiveFeed:
    def __init__(self, pairs: list[Pair], *, client: Any = None, kalshi_credentials: Any = None,
                 kalshi_poll_seconds: float = 2.0, poly_stream: Any = None, kalshi_stream: Any = None):
        self.pairs = pairs
        self.client = client or synpath.Client()
        self.kalshi_poll_seconds = kalshi_poll_seconds
        self.poly = poly_stream or synpath.PolymarketMarketStream()
        self.kalshi = kalshi_stream or (synpath.KalshiStream(kalshi_credentials) if kalshi_credentials else None)
        self._polled: dict[str, Book] = {}
        """Kalshi books read over REST, when there is no Kalshi stream."""
        self._changed: asyncio.Queue[Pair] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []
        self._by_market: dict[str, list[Pair]] = {}
        for pair in pairs:
            self._by_market.setdefault(pair.kalshi_id, []).append(pair)
            self._by_market.setdefault(pair.poly_id, []).append(pair)

    @property
    def kalshi_mode(self) -> str:
        return "WebSocket" if self.kalshi is not None else f"REST every {self.kalshi_poll_seconds:g}s (no Kalshi key)"

    async def start(self) -> None:
        self.poly.start()
        await self.poly.watch_order_book([p.poly_id for p in self.pairs])
        self._tasks.append(asyncio.create_task(self._pump(self.poly)))
        if self.kalshi is not None:
            self.kalshi.start()
            await self.kalshi.watch_order_book([p.kalshi_id for p in self.pairs])
            self._tasks.append(asyncio.create_task(self._pump(self.kalshi)))
        else:
            self._tasks.append(asyncio.create_task(self._poll_kalshi()))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        for stream in (self.poly, self.kalshi):
            if stream is not None:
                await stream.close()

    async def _pump(self, stream: Any) -> None:
        """Every book event names a market; the pairs that use it are marked changed."""
        async for event in stream:
            if isinstance(event, synpath.BookEvent):
                self._mark(event.market_id)
            elif isinstance(event, synpath.StreamStatusEvent) and event.state in ("gap", "disconnected", "failed"):
                print(f"[FEED] {event.venue} {event.state}{': ' + event.detail if event.detail else ''}")

    async def _poll_kalshi(self) -> None:
        while True:
            for market_id in {p.kalshi_id for p in self.pairs}:
                try:
                    book = await asyncio.to_thread(rest_top, self.client, market_id)
                except Exception as exc:
                    print(f"[FEED] kalshi {market_id}: {type(exc).__name__}: {exc}")
                    continue
                if book != self._polled.get(market_id):
                    self._polled[market_id] = book
                    self._mark(market_id)
            await asyncio.sleep(self.kalshi_poll_seconds)

    def _mark(self, market_id: str) -> None:
        for pair in self._by_market.get(market_id, []):
            self._changed.put_nowait(pair)

    async def changes(self) -> AsyncIterator[Pair]:
        """The pairs whose books moved, as they move. A burst of changes to one pair is
        collapsed into one, so the bot prices the latest books rather than every step."""
        while True:
            pair = await self._changed.get()
            pending = {pair.kalshi_id: pair}
            while not self._changed.empty():
                more = self._changed.get_nowait()
                pending[more.kalshi_id] = more
            for p in pending.values():
                yield p

    def books(self, pair: Pair) -> tuple[Book, Book] | None:
        """Both venues' top of book in this market's terms, or None until both are known."""
        kalshi = top_of(self.kalshi.book(pair.kalshi_id)) if self.kalshi is not None else self._polled.get(pair.kalshi_id)
        poly = top_of(self.poly.book(pair.poly_id))
        if kalshi is None or poly is None:
            return None
        return kalshi, (poly.flipped() if pair.flipped else poly)
