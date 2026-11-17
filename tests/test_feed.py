
import pytest
import synpath
import asyncio
from src.matcher import Pair
from trading.feed import LiveFeed

from types import SimpleNamespace

from src.arbitrage import Book
from trading.feed import top_of, rest_top


def test_top_of_returns_none_when_book_is_missing():
    assert top_of(None) is None


def test_top_of_returns_none_when_book_is_not_ready():
    book = SimpleNamespace(ready=False)

    assert top_of(book) is None


def test_top_of_converts_prices_and_quantities():
    book = SimpleNamespace(
        ready=True,
        best_bid=0.40,
        best_ask=0.42,
        bids={0.40: 100},
        asks={0.42: 50},
    )

    result = top_of(book)

    assert result == Book(
        0.40,
        0.42,
        bid_size=100.0,
        ask_size=50.0,
    )


def test_top_of_handles_missing_bid_and_ask():
    book = SimpleNamespace(
        ready=True,
        best_bid=None,
        best_ask=None,
        bids={},
        asks={},
    )

    result = top_of(book)

    assert result == Book(None, None, None, None)


def test_rest_top_fetches_and_converts_order_book():
    class FakeClient:
        def fetch_order_book(self, market_id, *, side, depth):
            assert market_id == "TEST-MARKET"
            assert side == "yes"
            assert depth == 1

            return SimpleNamespace(
                best_bid=SimpleNamespace(price=0.40, size=100),
                best_ask=SimpleNamespace(price=0.42, size=50),
            )

    result = rest_top(FakeClient(), "TEST-MARKET")

    assert result == Book(
        0.40,
        0.42,
        bid_size=100,
        ask_size=50,
    )


def test_rest_top_handles_empty_order_book():
    class FakeClient:
        def fetch_order_book(self, market_id, *, side, depth):
            return SimpleNamespace(
                best_bid=None,
                best_ask=None,
            )

    result = rest_top(FakeClient(), "TEST-MARKET")

    assert result == Book(None, None, None, None)

def test_rest_poll_invalidates_stale_book_and_recovers():
    async def scenario():
        pair = Pair(
            "e",
            "Test Event",
            "Outcome",
            "kalshi:TEST",
            "polymarket:1",
            False,
            "same",
        )

        class FakeClient:
            def __init__(self):
                self.calls = 0

            def fetch_order_book(self, market_id, *, side, depth):
                self.calls += 1

                if self.calls == 1:
                    return SimpleNamespace(
                        best_bid=SimpleNamespace(price=0.40, size=100),
                        best_ask=SimpleNamespace(price=0.42, size=100),
                    )

                if self.calls == 2:
                    raise RuntimeError("Temporary REST failure")

                return SimpleNamespace(
                    best_bid=SimpleNamespace(price=0.41, size=100),
                    best_ask=SimpleNamespace(price=0.43, size=100),
                )

        feed = LiveFeed(
            [pair],
            client=FakeClient(),
            kalshi_poll_seconds=0.01,
        )

        observed_books = []
        original_mark = feed._mark

        def capture_mark(market_id):
            observed_books.append(feed._polled.get(market_id))
            original_mark(market_id)

        feed._mark = capture_mark

        task = asyncio.create_task(feed._poll_kalshi())

        try:
            for _ in range(200):
                if len(observed_books) >= 3:
                    break
                await asyncio.sleep(0.01)

            assert len(observed_books) >= 3

            # First poll: valid book received.
            assert observed_books[0] == Book(
                0.40, 0.42, 100, 100
            )

            # Second poll: failed request invalidates the cached book.
            assert observed_books[1] is None

            # Third poll: successful request restores valid data.
            assert observed_books[2] == Book(
                0.41, 0.43, 100, 100
            )

            assert feed._polled["kalshi:TEST"] == Book(
                0.41, 0.43, 100, 100
            )

        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

def test_live_feed_rejects_unready_websocket_book_and_recovers():
    pair = Pair(
        "e",
        "Test Event",
        "Outcome",
        "kalshi:TEST",
        "polymarket:1",
        False,
        "same",
    )

    class FakePolyStream:
        def __init__(self):
            self.current_book = SimpleNamespace(
                ready=True,
                best_bid=0.26,
                best_ask=0.27,
                bids={0.26: 100},
                asks={0.27: 80},
            )

        def book(self, market_id):
            assert market_id == "polymarket:1"
            return self.current_book

    fake_stream = FakePolyStream()

    feed = LiveFeed(
        [pair],
        client=SimpleNamespace(),
        poly_stream=fake_stream,
    )

    feed._polled["kalshi:TEST"] = Book(
        0.40, 0.42, 100, 100
    )

    # Initially, both books are ready.
    result = feed.books(pair)

    assert result == (
        Book(0.40, 0.42, 100, 100),
        Book(0.26, 0.27, 100.0, 80.0),
    )

    # Simulate a WebSocket interruption.
    fake_stream.current_book.ready = False

    assert feed.books(pair) is None

    # Simulate reconnection and receipt of a fresh snapshot.
    fake_stream.current_book = SimpleNamespace(
        ready=True,
        best_bid=0.28,
        best_ask=0.29,
        bids={0.28: 120},
        asks={0.29: 90},
    )

    recovered = feed.books(pair)

    assert recovered == (
        Book(0.40, 0.42, 100, 100),
        Book(0.28, 0.29, 120.0, 90.0),
    )

def test_websocket_disconnect_invalidates_books_until_fresh_snapshot():
    async def scenario():
        pair = Pair(
            "e",
            "Test Event",
            "Outcome",
            "kalshi:TEST",
            "polymarket:1",
            False,
            "same",
        )

        class FakePolyStream:
            def __init__(self):
                self.events = asyncio.Queue()
                self.current_book = SimpleNamespace(
                    ready=True,
                    best_bid=0.26,
                    best_ask=0.27,
                    bids={0.26: 100},
                    asks={0.27: 80},
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.events.get()

            def book(self, market_id):
                return self.current_book

        stream = FakePolyStream()

        feed = LiveFeed(
            [pair],
            client=SimpleNamespace(),
            poly_stream=stream,
        )

        feed._polled["kalshi:TEST"] = Book(
            0.40, 0.42, 100, 100
        )

        # Initially both books are usable.
        assert feed.books(pair) is not None

        task = asyncio.create_task(feed._pump(stream))

        async def wait_until(predicate):
            for _ in range(100):
                if predicate():
                    return
                await asyncio.sleep(0.001)

            raise AssertionError("Feed event was not processed")

        try:
            await stream.events.put(
                synpath.StreamStatusEvent(
                    venue="polymarket",
                    stream="market_data",
                    state="disconnected",
                    detail="test disconnect",
                )
            )

            await wait_until(
                lambda: pair.poly_id in feed._unready_markets
            )

            # Cached prices must not be used after disconnection.
            assert feed.books(pair) is None

            # Simulate a new snapshot after reconnection.
            stream.current_book = SimpleNamespace(
                ready=True,
                best_bid=0.28,
                best_ask=0.29,
                bids={0.28: 120},
                asks={0.29: 90},
            )

            await stream.events.put(
                synpath.BookEvent(
                    venue="polymarket",
                    market_id=pair.poly_id,
                    kind="snapshot",
                    side="yes",
                )
            )

            await wait_until(
                lambda: pair.poly_id not in feed._unready_markets
            )

            recovered = feed.books(pair)

            assert recovered == (
                Book(0.40, 0.42, 100, 100),
                Book(0.28, 0.29, 120.0, 90.0),
            )

        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())


def test_failed_websocket_stream_stays_unavailable():
    async def scenario():
        pair = Pair(
            "e",
            "Test Event",
            "Outcome",
            "kalshi:TEST",
            "polymarket:1",
            False,
            "same",
        )

        class FakePolyStream:
            def __init__(self):
                self.events = asyncio.Queue()
                self.current_book = SimpleNamespace(
                    ready=True,
                    best_bid=0.26,
                    best_ask=0.27,
                    bids={0.26: 100},
                    asks={0.27: 80},
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.events.get()

            def book(self, market_id):
                return self.current_book

        stream = FakePolyStream()

        feed = LiveFeed(
            [pair],
            client=SimpleNamespace(),
            poly_stream=stream,
        )

        feed._polled["kalshi:TEST"] = Book(
            0.40, 0.42, 100, 100
        )

        task = asyncio.create_task(feed._pump(stream))

        async def wait_until(predicate):
            for _ in range(100):
                if predicate():
                    return
                await asyncio.sleep(0.001)

            raise AssertionError("Feed event was not processed")

        try:
            await stream.events.put(
                synpath.StreamStatusEvent(
                    venue="polymarket",
                    stream="market_data",
                    state="failed",
                    detail="terminal test failure",
                )
            )

            await wait_until(
                lambda: "polymarket" in feed._failed_venues
            )

            assert feed.books(pair) is None

            # Even a late snapshot must not revive a terminally failed stream.
            await stream.events.put(
                synpath.BookEvent(
                    venue="polymarket",
                    market_id=pair.poly_id,
                    kind="snapshot",
                    side="yes",
                )
            )

            await asyncio.sleep(0.01)

            assert pair.poly_id in feed._unready_markets
            assert feed.books(pair) is None

        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

def test_websocket_gap_rejects_book_until_fresh_snapshot():
    async def scenario():
        pair = Pair(
            "e",
            "Test Event",
            "Outcome",
            "kalshi:TEST",
            "polymarket:1",
            False,
            "same",
        )

        class FakePolyStream:
            def __init__(self):
                self.events = asyncio.Queue()
                self.current_book = SimpleNamespace(
                    ready=True,
                    best_bid=0.26,
                    best_ask=0.27,
                    bids={0.26: 100},
                    asks={0.27: 80},
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.events.get()

            def book(self, market_id):
                return self.current_book

        stream = FakePolyStream()

        feed = LiveFeed(
            [pair],
            client=SimpleNamespace(),
            poly_stream=stream,
        )

        feed._polled["kalshi:TEST"] = Book(
            0.40, 0.42, 100, 100
        )

        task = asyncio.create_task(feed._pump(stream))

        try:
            # Simulate Synpath detecting a sequence gap.
            stream.current_book.ready = False

            await stream.events.put(
                synpath.StreamStatusEvent(
                    venue="polymarket",
                    stream="market_data",
                    state="gap",
                    detail="sequence gap detected",
                    key=pair.poly_id,
                    reconcile_required=True,
                )
            )

            await asyncio.sleep(0.01)

            # The unready book must not reach the strategy.
            assert feed.books(pair) is None

            # Simulate successful reconciliation and a fresh snapshot.
            stream.current_book = SimpleNamespace(
                ready=True,
                best_bid=0.28,
                best_ask=0.29,
                bids={0.28: 120},
                asks={0.29: 90},
            )

            await stream.events.put(
                synpath.BookEvent(
                    venue="polymarket",
                    market_id=pair.poly_id,
                    kind="snapshot",
                    side="yes",
                )
            )

            await asyncio.sleep(0.01)

            recovered = feed.books(pair)

            assert recovered == (
                Book(0.40, 0.42, 100, 100),
                Book(0.28, 0.29, 120.0, 90.0),
            )

        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

def test_close_waits_for_background_tasks_before_closing_streams():
    async def scenario():
        class FakeStream:
            def __init__(self):
                self.closed = False
                self.task = None

            async def close(self):
                # The background task must finish before the connection closes.
                assert self.task.done()
                self.closed = True

        poly_stream = FakeStream()
        kalshi_stream = FakeStream()

        feed = LiveFeed(
            [],
            client=SimpleNamespace(),
            poly_stream=poly_stream,
            kalshi_stream=kalshi_stream,
        )

        # Create a background task that waits indefinitely.
        never = asyncio.Event()
        task = asyncio.create_task(never.wait())

        poly_stream.task = task
        kalshi_stream.task = task
        feed._tasks.append(task)

        await feed.close()

        assert task.done()
        assert task.cancelled()
        assert poly_stream.closed
        assert kalshi_stream.closed
        assert feed._tasks == []

    asyncio.run(scenario())

def test_gap_event_invalidates_book_even_when_stream_still_reports_ready():
    async def scenario():
        pair = Pair(
            "e",
            "Test Event",
            "Outcome",
            "kalshi:TEST",
            "polymarket:1",
            False,
            "same",
        )

        class FakePolyStream:
            def __init__(self):
                self.events = asyncio.Queue()
                self.current_book = SimpleNamespace(
                    ready=True,
                    best_bid=0.26,
                    best_ask=0.27,
                    bids={0.26: 100},
                    asks={0.27: 80},
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                return await self.events.get()

            def book(self, market_id):
                return self.current_book

        stream = FakePolyStream()

        feed = LiveFeed(
            [pair],
            client=SimpleNamespace(),
            poly_stream=stream,
        )

        feed._polled["kalshi:TEST"] = Book(
            0.40, 0.42, 100, 100
        )

        task = asyncio.create_task(feed._pump(stream))

        try:
            assert feed.books(pair) is not None

            await stream.events.put(
                synpath.StreamStatusEvent(
                    venue="polymarket",
                    stream="market_data",
                    state="gap",
                    detail="sequence gap detected",
                    key=pair.poly_id,
                    reconcile_required=True,
                )
            )

            for _ in range(100):
                if pair.poly_id in feed._unready_markets:
                    break
                await asyncio.sleep(0.001)

            assert pair.poly_id in feed._unready_markets
            assert feed.books(pair) is None

            # A fresh snapshot should restore the book.
            stream.current_book = SimpleNamespace(
                ready=True,
                best_bid=0.28,
                best_ask=0.29,
                bids={0.28: 120},
                asks={0.29: 90},
            )

            await stream.events.put(
                synpath.BookEvent(
                    venue="polymarket",
                    market_id=pair.poly_id,
                    kind="snapshot",
                    side="yes",
                )
            )

            for _ in range(100):
                if pair.poly_id not in feed._unready_markets:
                    break
                await asyncio.sleep(0.001)

            recovered = feed.books(pair)

            assert recovered == (
                Book(0.40, 0.42, 100, 100),
                Book(0.28, 0.29, 120.0, 90.0),
            )

        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(scenario())

def test_start_closes_streams_if_second_venue_startup_fails():
    async def scenario():
        pair = Pair(
            "e",
            "Test Event",
            "Outcome",
            "kalshi:TEST",
            "polymarket:1",
            False,
            "same",
        )

        class FakeStream:
            def __init__(self, fail_watch=False):
                self.fail_watch = fail_watch
                self.closed = False

            def start(self):
                pass

            async def watch_order_book(self, market_ids):
                if self.fail_watch:
                    raise RuntimeError("Simulated Kalshi startup failure")

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.Future()

            async def close(self):
                self.closed = True

            def book(self, market_id):
                return None

        poly = FakeStream()
        kalshi = FakeStream(fail_watch=True)

        feed = LiveFeed(
            [pair],
            client=SimpleNamespace(),
            poly_stream=poly,
            kalshi_stream=kalshi,
        )

        try:
            with pytest.raises(
                RuntimeError,
                match="Simulated Kalshi startup failure",
            ):
                await feed.start()

            assert poly.closed
            assert kalshi.closed
            assert feed._tasks == []

        finally:
            await feed.close()

    asyncio.run(scenario())

def test_close_attempts_all_streams_when_one_close_fails():
    async def scenario():
        class FakeStream:
            def __init__(self, fail_close=False):
                self.fail_close = fail_close
                self.closed = False

            async def close(self):
                self.closed = True

                if self.fail_close:
                    raise RuntimeError("Simulated close failure")

        poly = FakeStream(fail_close=True)
        kalshi = FakeStream()

        feed = LiveFeed(
            [],
            client=SimpleNamespace(),
            poly_stream=poly,
            kalshi_stream=kalshi,
        )

        with pytest.raises(
            RuntimeError,
            match="failed to close",
        ):
            await feed.close()

        assert poly.closed
        assert kalshi.closed
        assert feed._tasks == []

    asyncio.run(scenario())

def test_unexpected_websocket_exception_invalidates_venue_books():
    async def scenario():
        pair = Pair(
            "e",
            "Test Event",
            "Outcome",
            "kalshi:TEST",
            "polymarket:1",
            False,
            "same",
        )

        class FakePolyStream:
            def __init__(self):
                self.events = asyncio.Queue()
                self.current_book = SimpleNamespace(
                    ready=True,
                    best_bid=0.26,
                    best_ask=0.27,
                    bids={0.26: 100},
                    asks={0.27: 80},
                )

            def __aiter__(self):
                return self

            async def __anext__(self):
                event = await self.events.get()

                if isinstance(event, Exception):
                    raise event

                return event

            def book(self, market_id):
                return self.current_book

        stream = FakePolyStream()

        feed = LiveFeed(
            [pair],
            client=SimpleNamespace(),
            poly_stream=stream,
        )

        feed._polled["kalshi:TEST"] = Book(
            0.40, 0.42, 100, 100
        )

        task = asyncio.create_task(feed._pump(stream))

        try:
            assert feed.books(pair) is not None

            await stream.events.put(
                RuntimeError("Unexpected WebSocket failure")
            )

            await asyncio.wait_for(task, timeout=1)

            assert "polymarket" in feed._failed_venues
            assert pair.poly_id in feed._unready_markets
            assert feed.books(pair) is None

            assert feed._changed.get_nowait() == pair

        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(
                    task,
                    return_exceptions=True,
                )

    asyncio.run(scenario())