import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from synpath import Side

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


def make_bot(tmp_path, books, kalshi_fee, poly_fee, *, flipped=False, contracts=1000):
    config = {"entry_edge": 0.02, "take_profit": 0.02, "contracts": contracts, "require_same_rules": True,
              "poll_interval_seconds": 1, "dry_run": True, "markets": [],
              "state_file": str(tmp_path / "positions.json"), "trades_file": str(tmp_path / "trades.csv")}
    bot = ArbitrageBot(config, client=FakeClient(books))
    pair = Pair("e", "Test Race", "Republican wins", "kalshi:TEST-R", "polymarket:1", flipped, "same")
    bot.pairs = [pair]
    bot.pricers[pair.kalshi_id] = Pricer(kalshi_fee, poly_fee, contracts=contracts)
    return bot, pair


def test_orders_follow_synpath_sides(tmp_path, kalshi_fee, poly_fee):
    bot, pair = make_bot(tmp_path, {}, kalshi_fee, poly_fee)
    buy_yes = bot.order(pair, "kalshi", "yes", 0.40, 10, closing=False)
    buy_no = bot.order(pair, "polymarket", "no", 0.54, 10, closing=False)
    sell_yes = bot.order(pair, "kalshi", "yes", 0.45, 10, closing=True)
    sell_no = bot.order(pair, "polymarket", "no", 0.54, 10, closing=True)
    assert (buy_yes.side, buy_yes.price, buy_yes.reduce_only) == (Side.BUY, Decimal("0.4"), False)
    assert (buy_no.side, buy_no.price, buy_no.reduce_only) == (Side.SELL, Decimal("0.46"), False)   # NO at 0.54 = YES 0.46
    assert (sell_yes.side, sell_yes.price, sell_yes.reduce_only) == (Side.SELL, Decimal("0.45"), True)
    assert (sell_no.side, sell_no.price, sell_no.reduce_only) == (Side.BUY, Decimal("0.46"), True)


def test_a_flipped_polymarket_trades_its_other_token(tmp_path, kalshi_fee, poly_fee):
    bot, pair = make_bot(tmp_path, {}, kalshi_fee, poly_fee, flipped=True)
    # Our NO on a venue that states the proposition the other way round is its YES.
    order = bot.order(pair, "polymarket", "no", 0.54, 10, closing=False)
    assert (order.side, order.price) == (Side.BUY, Decimal("0.54"))


def test_dry_run_opens_holds_and_closes(tmp_path, kalshi_fee, poly_fee):
    books = {"kalshi:TEST-R": ((0.39, 500), (0.40, 800)), "polymarket:1": ((0.46, 300), (0.47, 900))}
    bot, pair = make_bot(tmp_path, books, kalshi_fee, poly_fee)
    asyncio.run(bot.poll())
    pos = bot.positions[pair.kalshi_id]
    assert pos.contracts == 300                       # capped by the NO leg's depth
    assert (tmp_path / "positions.json").exists()

    books["kalshi:TEST-R"] = ((0.41, 500), (0.42, 500))   # narrower but not the target: hold
    asyncio.run(bot.poll())
    assert pair.kalshi_id in bot.positions

    books["kalshi:TEST-R"] = ((0.48, 500), (0.49, 500))   # Kalshi overshoots: sell both legs
    books["polymarket:1"] = ((0.44, 500), (0.45, 500))
    asyncio.run(bot.poll())
    assert pair.kalshi_id not in bot.positions
    rows = (tmp_path / "trades.csv").read_text().splitlines()
    assert len(rows) == 2 and rows[1].split(",")[4] == "300.0"      # header + the one round trip


def test_uneven_fills_keep_only_matched_pairs(tmp_path, kalshi_fee, poly_fee):
    books = {"kalshi:TEST-R": ((0.39, 500), (0.40, 800)), "polymarket:1": ((0.46, 300), (0.47, 900))}
    bot, pair = make_bot(tmp_path, books, kalshi_fee, poly_fee)
    sent = []

    async def send(venue, request):
        sent.append((venue, request))
        if request.reduce_only:
            return float(request.amount)
        return 300.0 if venue == "kalshi" else 120.0       # the Polymarket leg fills only 120

    bot.send = send
    asyncio.run(bot.poll())
    assert bot.positions[pair.kalshi_id].contracts == 120
    trims = [r for v, r in sent if r.reduce_only]
    assert len(trims) == 1 and trims[0].amount == Decimal("180.0") and trims[0].market_id == "kalshi:TEST-R"
