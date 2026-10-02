import asyncio
from decimal import Decimal
from types import SimpleNamespace

from synpath import BookEvent, LocalBook

from src.arbitrage import Book, Pricer
from src.matcher import Pair
from trading.bot import ArbitrageBot
from trading.feed import LiveFeed, top_of


def level(price, size):
    return SimpleNamespace(price=price, size=size)


class FakeClient:
    """Order books keyed by market id; YES side, top of book only."""
    def __init__(self, books):
        self.books = books

    def fetch_order_book(self, market_id, side="yes", depth=1):
        bid, ask = self.books[market_id]
        return SimpleNamespace(best_bid=level(*bid) if bid else None, best_ask=level(*ask) if ask else None)


class FakeStream:
    """A venue stream: local books by market id, and the events a test feeds it."""
    def __init__(self):
        self.local: dict[str, LocalBook] = {}
        self.events: asyncio.Queue = asyncio.Queue()
        self.watched: list[str] = []

    def start(self):
        return self

    async def watch_order_book(self, market_ids):
        self.watched += market_ids

    def book(self, market_id, side="yes"):
        return self.local.get(market_id)

    def set(self, market_id, bid, ask):
        book = LocalBook()
        book.replace([(Decimal(str(bid[0])), Decimal(str(bid[1])))], [(Decimal(str(ask[0])), Decimal(str(ask[1])))])
        self.local[market_id] = book
        self.events.put_nowait(BookEvent(venue="test", market_id=market_id, kind="snapshot"))

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.events.get()

    async def close(self):
        pass


PAIR = Pair("e", "Test Event", "Outcome", "kalshi:TEST", "polymarket:1", False, "same")


def make_bot(tmp_path, kalshi_fee, poly_fee):
    config = {"entry_edge": 0.02, "take_profit": 0.02, "contracts": 1000, "require_same_rules": True,
              "status_interval_seconds": 60, "markets": [],
              "state_file": str(tmp_path / "positions.json"), "trades_file": str(tmp_path / "trades.csv")}
    bot = ArbitrageBot(config, client=SimpleNamespace())
    bot.pairs = [PAIR]
    bot.pricers[PAIR.kalshi_id] = Pricer(kalshi_fee, poly_fee, contracts=1000)
    return bot


def test_opens_holds_and_closes_a_paper_position(tmp_path, kalshi_fee, poly_fee):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    bot.evaluate(PAIR, Book(0.39, 0.40, 500, 800), Book(0.46, 0.47, 300, 900))
    pos = bot.positions[PAIR.kalshi_id]
    assert (pos.yes_venue, pos.yes_price, pos.no_venue, pos.no_price) == ("kalshi", 0.40, "polymarket", 0.54)
    assert pos.contracts == 300                        # capped by the size quoted on the NO leg
    assert (tmp_path / "positions.json").exists()

    bot.evaluate(PAIR, Book(0.41, 0.42, 500, 500), Book(0.46, 0.47, 300, 900))   # short of the target: hold
    assert PAIR.kalshi_id in bot.positions

    bot.evaluate(PAIR, Book(0.48, 0.49, 500, 500), Book(0.44, 0.45, 500, 500))   # the round trip clears it
    assert PAIR.kalshi_id not in bot.positions
    rows = (tmp_path / "trades.csv").read_text().splitlines()
    assert len(rows) == 2 and float(rows[1].split(",")[-2]) >= 0.02


def test_positions_survive_a_restart(tmp_path, kalshi_fee, poly_fee):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    bot.evaluate(PAIR, Book(0.39, 0.40, 500, 800), Book(0.46, 0.47, 300, 900))
    again = make_bot(tmp_path, kalshi_fee, poly_fee)
    again.load_state()
    assert again.positions[PAIR.kalshi_id].contracts == 300


def test_status_lines_are_rate_limited(tmp_path, kalshi_fee, poly_fee, capsys):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    for _ in range(5):                                  # books move often; no edge here
        bot.evaluate(PAIR, Book(0.44, 0.45, 500, 500), Book(0.44, 0.45, 500, 500))
    assert capsys.readouterr().out.count("[Test Event") == 1


def test_a_stream_book_is_read_only_once_ready():
    book = LocalBook()
    assert top_of(book) is None                        # no snapshot yet
    book.replace([(Decimal("0.39"), Decimal("500"))], [(Decimal("0.40"), Decimal("800"))])
    assert top_of(book) == Book(0.39, 0.40, 500.0, 800.0)
    book.invalidate()                                  # a gap: not trusted until repaired
    assert top_of(book) is None


def test_the_feed_prices_a_pair_when_either_book_moves():
    async def run():
        poly, kalshi = FakeStream(), FakeStream()
        flipped = Pair("e", "Flipped", "Outcome", "kalshi:TEST", "polymarket:1", True, "same")
        feed = LiveFeed([flipped], client=SimpleNamespace(), poly_stream=poly, kalshi_stream=kalshi)
        await feed.start()
        assert poly.watched == ["polymarket:1"] and kalshi.watched == ["kalshi:TEST"]
        changes = feed.changes()
        kalshi.set("kalshi:TEST", (0.39, 500), (0.40, 800))
        assert (await asyncio.wait_for(changes.__anext__(), 1)) == flipped
        assert feed.books(flipped) is None             # Polymarket's book not known yet
        poly.set("polymarket:1", (0.54, 900), (0.55, 300))
        assert (await asyncio.wait_for(changes.__anext__(), 1)) == flipped
        k, p = feed.books(flipped)
        assert k == Book(0.39, 0.40, 500.0, 800.0)
        # Polymarket states it the other way round: its 0.54/0.55 YES is our 0.45/0.46.
        assert (p.bid, p.ask, p.bid_size) == (0.45, 0.46, 300.0)
        await feed.close()
    asyncio.run(run())


def test_without_a_kalshi_key_kalshi_is_read_over_rest():
    async def run():
        client = FakeClient({"kalshi:TEST": ((0.39, 500), (0.40, 800))})
        feed = LiveFeed([PAIR], client=client, poly_stream=FakeStream(), kalshi_stream=None, kalshi_poll_seconds=0.01)
        assert feed.kalshi is None and "REST" in feed.kalshi_mode
        await feed.start()
        changes = feed.changes()
        assert (await asyncio.wait_for(changes.__anext__(), 1)) == PAIR
        assert feed._polled["kalshi:TEST"] == Book(0.39, 0.40, 500, 800)
        await feed.close()
    asyncio.run(run())
