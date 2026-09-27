import asyncio
from types import SimpleNamespace

from src.arbitrage import Pricer
from src.bot import ArbitrageBot
from src.matcher import Pair


def level(price, size):
    return SimpleNamespace(price=price, size=size)


class FakeClient:
    """Order books keyed by market id; YES side, top of book only."""
    def __init__(self, books):
        self.books = books

    def fetch_order_book(self, market_id, side="yes", depth=1):
        bid, ask = self.books[market_id]
        return SimpleNamespace(best_bid=level(*bid) if bid else None, best_ask=level(*ask) if ask else None)


def make_bot(tmp_path, books, kalshi_fee, poly_fee, *, flipped=False):
    config = {"entry_edge": 0.02, "take_profit": 0.02, "contracts": 1000, "require_same_rules": True,
              "poll_interval_seconds": 1, "markets": [],
              "state_file": str(tmp_path / "positions.json"), "trades_file": str(tmp_path / "trades.csv")}
    bot = ArbitrageBot(config, client=FakeClient(books))
    pair = Pair("e", "Test Event", "Outcome", "kalshi:TEST", "polymarket:1", flipped, "same")
    bot.pairs = [pair]
    bot.pricers[pair.kalshi_id] = Pricer(kalshi_fee, poly_fee, contracts=1000)
    return bot, pair


def test_opens_holds_and_closes_a_paper_position(tmp_path, kalshi_fee, poly_fee):
    books = {"kalshi:TEST": ((0.39, 500), (0.40, 800)), "polymarket:1": ((0.46, 300), (0.47, 900))}
    bot, pair = make_bot(tmp_path, books, kalshi_fee, poly_fee)
    asyncio.run(bot.poll())
    pos = bot.positions[pair.kalshi_id]
    assert (pos.yes_venue, pos.yes_price, pos.no_venue, pos.no_price) == ("kalshi", 0.40, "polymarket", 0.54)
    assert pos.contracts == 300                        # capped by the size quoted on the NO leg
    assert (tmp_path / "positions.json").exists()

    books["kalshi:TEST"] = ((0.41, 500), (0.42, 500))  # narrower but short of the target: hold
    asyncio.run(bot.poll())
    assert pair.kalshi_id in bot.positions

    books["kalshi:TEST"] = ((0.48, 500), (0.49, 500))  # the round trip clears the target: exit
    books["polymarket:1"] = ((0.44, 500), (0.45, 500))
    asyncio.run(bot.poll())
    assert pair.kalshi_id not in bot.positions
    rows = (tmp_path / "trades.csv").read_text().splitlines()
    assert len(rows) == 2 and float(rows[1].split(",")[-2]) >= 0.02


def test_positions_survive_a_restart(tmp_path, kalshi_fee, poly_fee):
    books = {"kalshi:TEST": ((0.39, 500), (0.40, 800)), "polymarket:1": ((0.46, 300), (0.47, 900))}
    bot, pair = make_bot(tmp_path, books, kalshi_fee, poly_fee)
    asyncio.run(bot.poll())
    again, _ = make_bot(tmp_path, books, kalshi_fee, poly_fee)
    again.load_state()
    assert again.positions[pair.kalshi_id].contracts == 300


def test_a_flipped_polymarket_book_is_read_from_the_other_side(tmp_path, kalshi_fee, poly_fee):
    # Polymarket states the proposition the other way round: its YES at 0.54/0.55 is our YES at 0.45/0.46.
    books = {"kalshi:TEST": ((0.39, 500), (0.40, 800)), "polymarket:1": ((0.54, 900), (0.55, 300))}
    bot, pair = make_bot(tmp_path, books, kalshi_fee, poly_fee, flipped=True)
    kalshi, poly = bot.books(pair)
    assert (poly.bid, poly.ask, poly.bid_size) == (0.45, 0.46, 300)
