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
        # Markets whose stream data must not be trusted until a fresh snapshot arrives.
        self._unready_markets: set[str] = set()

        # A failed stream is terminal and must remain unavailable.
        self._failed_venues: set[str] = set()
        self._by_market: dict[str, list[Pair]] = {}
        for pair in pairs:
            self._by_market.setdefault(pair.kalshi_id, []).append(pair)
            self._by_market.setdefault(pair.poly_id, []).append(pair)

    @property
    def kalshi_mode(self) -> str:
        return "WebSocket" if self.kalshi is not None else f"REST every {self.kalshi_poll_seconds:g}s (no Kalshi key)"

    async def start(self) -> None:
        try:
            self.poly.start()
            await self.poly.watch_order_book(
                [p.poly_id for p in self.pairs]
            )
            self._tasks.append(
                asyncio.create_task(self._pump(self.poly))
            )

            if self.kalshi is not None:
                self.kalshi.start()
                await self.kalshi.watch_order_book(
                    [p.kalshi_id for p in self.pairs]
                )
                self._tasks.append(
                    asyncio.create_task(self._pump(self.kalshi))
                )
            else:
                self._tasks.append(
                    asyncio.create_task(self._poll_kalshi())
                )

        except BaseException:
            try:
                await self.close()
            except Exception as cleanup_exc:
                print(
                    f"[FEED] Cleanup after startup failure: "
                    f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
            raise

    async def close(self) -> None:
        """Cancel background tasks and attempt to close every stream."""
        tasks = self._tasks
        self._tasks = []

        # Cancel all background tasks first.
        for task in tasks:
            task.cancel()

        # Wait for all background tasks to finish.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        # Attempt every connection close, even if one fails.
        close_errors = []

        for stream in (self.poly, self.kalshi):
            if stream is None:
                continue

            try:
                await stream.close()
            except Exception as exc:
                close_errors.append(exc)
                print(
                    f"[FEED] Stream close failed: "
                    f"{type(exc).__name__}: {exc}"
                )

        if close_errors:
            raise RuntimeError(
                "One or more feed streams failed to close"
            ) from close_errors[0]

    async def _pump(self, stream: Any) -> None:
        """Handle book updates and invalidate a venue after stream failure."""

        if stream is self.poly:
            stream_venue = "polymarket"
        elif stream is self.kalshi:
            stream_venue = "kalshi"
        else:
            raise ValueError("Unknown market-data stream")

        try:
            async for event in stream:
                if isinstance(event, synpath.BookEvent):
                    # A fresh YES snapshot restores trust in this market,
                    # unless the venue stream has permanently failed.
                    if (
                        event.kind == "snapshot"
                        and event.side == "yes"
                        and event.venue not in self._failed_venues
                    ):
                        self._unready_markets.discard(event.market_id)

                    self._mark(event.market_id)

                elif isinstance(event, synpath.StreamStatusEvent):
                    if event.state in (
                        "gap",
                        "disconnected",
                        "failed",
                    ):
                        print(
                            f"[FEED] {event.venue} {event.state}"
                            f"{': ' + event.detail if event.detail else ''}"
                        )

                    if event.state == "gap":
                        # Invalidate the affected market after a sequence gap.
                        market_id = getattr(event, "key", None)

                        if (
                            isinstance(market_id, str)
                            and market_id in self._by_market
                        ):
                            affected_markets = {market_id}
                        else:
                            affected_markets = {
                                market
                                for market in self._by_market
                                if market.startswith(f"{event.venue}:")
                            }

                        self._unready_markets.update(affected_markets)

                        for market_id in affected_markets:
                            self._mark(market_id)

                    elif event.state == "disconnected":
                        self._invalidate_venue(event.venue)

                    elif event.state == "failed":
                        self._invalidate_venue(
                            event.venue,
                            terminal=True,
                        )

        except asyncio.CancelledError:
            # Normal shutdown must not be treated as a stream failure.
            raise

        except Exception as exc:
            print(
                f"[FEED] {stream_venue} stream terminated unexpectedly: "
                f"{type(exc).__name__}: {exc}"
            )

            self._invalidate_venue(
                stream_venue,
                terminal=True,
            )

        else:
            # An unexpected clean end is also a terminal stream failure.
            print(
                f"[FEED] {stream_venue} stream ended unexpectedly"
            )

            self._invalidate_venue(
                stream_venue,
                terminal=True,
            )

    async def _poll_kalshi(self) -> None:
        while True:
            for market_id in {p.kalshi_id for p in self.pairs}:
                try:
                    book = await asyncio.to_thread(
                        rest_top, self.client, market_id
                    )
                except Exception as exc:
                    print(
                        f"[FEED] kalshi {market_id}: "
                        f"{type(exc).__name__}: {exc}"
                    )

                    # Invalidate the cached quote after a failed REST request.
                    # Notify the bot if a previously valid quote was removed.
                    if self._polled.pop(market_id, None) is not None:
                        self._mark(market_id)

                    continue

                # A successful poll after a failure restores the book.
                if book != self._polled.get(market_id):
                    self._polled[market_id] = book
                    self._mark(market_id)

            await asyncio.sleep(self.kalshi_poll_seconds)

    def _invalidate_venue(
        self,
        venue: str,
        *,
        terminal: bool = False,
    ) -> None:
        """Invalidate every cached market belonging to an unavailable venue."""
        if terminal:
            self._failed_venues.add(venue)

        venue_market_ids = {
            market_id
            for market_id in self._by_market
            if market_id.startswith(f"{venue}:")
        }

        self._unready_markets.update(venue_market_ids)

        for market_id in venue_market_ids:
            self._mark(market_id)

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
        """Return both books only when their data is ready and trusted."""

        # Never use cached prices after a disconnect until fresh snapshots arrive.
        if (
            pair.kalshi_id in self._unready_markets
            or pair.poly_id in self._unready_markets
        ):
            return None

        kalshi = (
            top_of(self.kalshi.book(pair.kalshi_id))
            if self.kalshi is not None
            else self._polled.get(pair.kalshi_id)
        )

        poly = top_of(self.poly.book(pair.poly_id))

        if kalshi is None or poly is None:
            return None

        return kalshi, (poly.flipped() if pair.flipped else poly)
