
from types import SimpleNamespace

from src.arbitrage import Book
from src.matcher import Pair
from trading.feed import LiveFeed


def make_pair(*, flipped=False):
    return Pair(
        event_id="TEST-EVENT",
        event="Test event",
        outcome="Test outcome",
        kalshi_id="kalshi:TEST",
        poly_id="polymarket:TEST",
        flipped=flipped,
        rules="same",
    )


class FakePolyStream:
    def __init__(self, book=None):
        self.current_book = book

    def book(self, market_id):
        return self.current_book


class FakeKalshiStream:
    def __init__(self, book=None):
        self.current_book = book

    def book(self, market_id):
        return self.current_book


def make_ready_book(bid, ask, bid_size=100, ask_size=100):
    return SimpleNamespace(
        ready=True,
        best_bid=bid,
        best_ask=ask,
        bids={bid: bid_size},
        asks={ask: ask_size},
    )


def test_books_returns_none_when_kalshi_book_is_missing():
    pair = make_pair()

    feed = LiveFeed(
        [pair],
        client=object(),
        poly_stream=FakePolyStream(
            make_ready_book(0.45, 0.47)
        ),
        kalshi_stream=FakeKalshiStream(None),
    )

    assert feed.books(pair) is None


def test_books_returns_none_when_poly_book_is_missing():
    pair = make_pair()

    feed = LiveFeed(
        [pair],
        client=object(),
        poly_stream=FakePolyStream(None),
        kalshi_stream=FakeKalshiStream(
            make_ready_book(0.45, 0.47)
        ),
    )

    assert feed.books(pair) is None


def test_books_returns_both_books_when_ready():
    pair = make_pair()

    feed = LiveFeed(
        [pair],
        client=object(),
        poly_stream=FakePolyStream(
            make_ready_book(0.46, 0.48, 80, 60)
        ),
        kalshi_stream=FakeKalshiStream(
            make_ready_book(0.40, 0.42, 100, 50)
        ),
    )

    result = feed.books(pair)

    assert result == (
        Book(0.40, 0.42, 100.0, 50.0),
        Book(0.46, 0.48, 80.0, 60.0),
    )


def test_books_flips_polymarket_book_when_pair_is_flipped():
    pair = make_pair(flipped=True)

    feed = LiveFeed(
        [pair],
        client=object(),
        poly_stream=FakePolyStream(
            make_ready_book(0.30, 0.35, 40, 25)
        ),
        kalshi_stream=FakeKalshiStream(
            make_ready_book(0.40, 0.42, 100, 50)
        ),
    )

    result = feed.books(pair)

    assert result == (
        Book(0.40, 0.42, 100.0, 50.0),
        Book(0.65, 0.70, 25.0, 40.0),
    )


def test_mark_queues_pairs_for_matching_market():
    pair = make_pair()

    feed = LiveFeed(
        [pair],
        client=object(),
        poly_stream=FakePolyStream(),
        kalshi_stream=FakeKalshiStream(),
    )

    feed._mark(pair.kalshi_id)

    assert feed._changed.get_nowait() == pair


def test_mark_ignores_unknown_market():
    pair = make_pair()

    feed = LiveFeed(
        [pair],
        client=object(),
        poly_stream=FakePolyStream(),
        kalshi_stream=FakeKalshiStream(),
    )

    feed._mark("kalshi:UNKNOWN")

    assert feed._changed.empty()
