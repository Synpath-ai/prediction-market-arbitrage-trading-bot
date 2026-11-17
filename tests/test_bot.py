import asyncio
import json
import pytest
from decimal import Decimal
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from synpath import BookEvent, LocalBook

from src.arbitrage import Book, Pricer
from src.matcher import Pair
from trading.bot import ArbitrageBot, Position
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
    config = {
        "entry_edge": 0.02,
        "take_profit": 0.02,
        "contracts": 1000,
        "require_same_rules": True,
        "max_open_positions": 3,
        "max_capital_committed": 1000000.00,
        "max_unmatched_exposure": 1000000.00,
        "max_daily_loss": 1000000.00,
        "emergency_stop": False,
        "status_interval_seconds": 60,
        "markets": [],
        "state_file": str(tmp_path / "positions.json"),
        "trades_file": str(tmp_path / "trades.csv"),
        "event_file": str(tmp_path / "events.jsonl"),
    }
    bot = ArbitrageBot(config, client=SimpleNamespace())
    bot.pairs = [PAIR]
    bot.pricers[PAIR.kalshi_id] = Pricer(kalshi_fee, poly_fee, contracts=1000)
    return bot


def test_opens_holds_and_closes_a_paper_position(tmp_path, kalshi_fee, poly_fee):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    bot.evaluate(PAIR, Book(0.39, 0.40, 500, 300), Book(0.46, 0.47, 300, 900))
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



def test_partial_fill_closes_matched_pairs_and_preserves_unmatched_exposure(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    # YES fills 800 contracts; NO fills only 300.
    bot.evaluate(
        PAIR,
        Book(0.39, 0.40, 500, 800),
        Book(0.46, 0.47, 300, 900),
    )

    pos = bot.positions[PAIR.kalshi_id]

    assert pos.yes_filled == 800
    assert pos.no_filled == 300
    assert pos.contracts == 300
    assert pos.unmatched_yes == 500
    assert pos.unmatched_no == 0

    # The bot liquidates the 500 unmatched YES contracts first.
    # This consumes all 500 displayed YES exit contracts.
    bot.evaluate(
        PAIR,
        Book(0.48, 0.49, 500, 500),
        Book(0.44, 0.45, 500, 500),
    )

    remaining = bot.positions[PAIR.kalshi_id]

    # The 300 matched pairs remain open because YES exit depth is exhausted.
    assert remaining.yes_filled == 300
    assert remaining.no_filled == 300
    assert remaining.contracts == 300
    assert remaining.unmatched_yes == 0
    assert remaining.unmatched_no == 0

    # No matched trade was completed.
    assert not (tmp_path / "trades.csv").exists()

    # The unmatched liquidation was recorded separately.
    rows = (tmp_path / "trades_unmatched.csv").read_text().splitlines()
    assert len(rows) == 2
    assert rows[1].split(",")[5] == "500"




def test_positions_survive_a_restart(tmp_path, kalshi_fee, poly_fee):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    # YES fills 800; NO fills 300.
    bot.evaluate(
        PAIR,
        Book(0.39, 0.40, 500, 800),
        Book(0.46, 0.47, 300, 900),
    )

    original = bot.positions[PAIR.kalshi_id]

    assert original.yes_filled == 800
    assert original.no_filled == 300
    assert original.contracts == 300
    assert original.unmatched_yes == 500

    # Liquidate unmatched YES exposure first.
    bot.evaluate(
        PAIR,
        Book(0.48, 0.49, 500, 500),
        Book(0.44, 0.45, 500, 500),
    )

    remaining = bot.positions[PAIR.kalshi_id]

    # The 300 matched pairs remain open.
    assert remaining.contracts == 300
    assert remaining.yes_filled == 300
    assert remaining.no_filled == 300
    assert remaining.unmatched_yes == 0

    # Simulate restarting the bot.
    again = make_bot(tmp_path, kalshi_fee, poly_fee)
    again.load_state()

    restored = again.positions[PAIR.kalshi_id]

    assert restored.contracts == 300
    assert restored.yes_filled == 300
    assert restored.no_filled == 300
    assert restored.unmatched_yes == 0
    assert restored.unmatched_no == 0



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


def make_unmatched_position(
    *,
    yes_filled=0,
    no_filled=0,
):
    return Position(
        kalshi_id=PAIR.kalshi_id,
        direction="yes_kalshi_no_polymarket",
        yes_venue="kalshi",
        yes_price=0.40,
        yes_fee=0.01,
        no_venue="polymarket",
        no_price=0.54,
        no_fee=0.01,
        contracts=0,
        edge=0.02,
        opened_at="2026-11-17 10:00:00 UTC",
        yes_filled=yes_filled,
        no_filled=no_filled,
    )


def test_liquidates_unmatched_yes_exposure(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    pos = make_unmatched_position(yes_filled=5)
    bot.positions[PAIR.kalshi_id] = pos

    bot.liquidate_unmatched(
        PAIR,
        pos,
        Book(0.50, 0.51, 5, 5),
        Book(0.40, 0.41, 5, 5),
    )

    assert PAIR.kalshi_id not in bot.positions

    rows = (tmp_path / "trades_unmatched.csv").read_text().splitlines()
    assert len(rows) == 2
    assert rows[1].split(",")[3] == "yes"
    assert rows[1].split(",")[5] == "5"


def test_liquidates_unmatched_no_exposure(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    pos = make_unmatched_position(no_filled=5)
    bot.positions[PAIR.kalshi_id] = pos

    bot.liquidate_unmatched(
        PAIR,
        pos,
        Book(0.40, 0.41, 5, 5),
        Book(0.39, 0.42, 5, 5),
    )

    assert PAIR.kalshi_id not in bot.positions

    rows = (tmp_path / "trades_unmatched.csv").read_text().splitlines()
    assert len(rows) == 2
    assert rows[1].split(",")[3] == "no"
    assert rows[1].split(",")[5] == "5"


def test_partial_unmatched_liquidation_preserves_depth_and_restart(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    pos = make_unmatched_position(yes_filled=10)
    bot.positions[PAIR.kalshi_id] = pos

    # Only four contracts are available at the current YES bid.
    bot.liquidate_unmatched(
        PAIR,
        pos,
        Book(0.50, 0.51, 4, 10),
        Book(0.39, 0.40, 10, 10),
    )

    remaining = bot.positions[PAIR.kalshi_id]

    assert remaining.yes_filled == 6
    assert remaining.no_filled == 0
    assert remaining.unmatched_yes == 6
    assert remaining.yes_exit_depth_used == 4

    # The partial liquidation must be saved.
    again = make_bot(tmp_path, kalshi_fee, poly_fee)
    again.load_state()

    restored = again.positions[PAIR.kalshi_id]

    assert restored.yes_filled == 6
    assert restored.no_filled == 0
    assert restored.unmatched_yes == 6
    assert restored.yes_exit_depth_used == 4

    rows = (tmp_path / "trades_unmatched.csv").read_text().splitlines()
    assert len(rows) == 2
    assert rows[1].split(",")[5] == "4"


def test_matched_exit_respects_available_depth(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    pos = make_unmatched_position(
        yes_filled=10,
        no_filled=10,
    )
    pos.contracts = 10
    bot.positions[PAIR.kalshi_id] = pos

    # Only six contracts are available on each exit leg.
    bot.maybe_exit(
        PAIR,
        pos,
        Book(0.50, 0.51, 6, 10),
        Book(0.39, 0.40, 10, 6),
    )

    remaining = bot.positions[PAIR.kalshi_id]

    assert remaining.yes_filled == 4
    assert remaining.no_filled == 4
    assert remaining.contracts == 4
    assert remaining.yes_exit_depth_used == 6
    assert remaining.no_exit_depth_used == 6

    rows = (tmp_path / "trades.csv").read_text().splitlines()
    assert len(rows) == 2
    assert rows[1].split(",")[4] == "6"


def test_unmatched_and_matched_exits_share_depth(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    pos = make_unmatched_position(
        yes_filled=8,
        no_filled=3,
    )
    pos.contracts = 3
    bot.positions[PAIR.kalshi_id] = pos

    # Five unmatched YES contracts are liquidated first.
    # Two YES contracts remain available for matched exits.
    bot.maybe_exit(
        PAIR,
        pos,
        Book(0.50, 0.51, 7, 10),
        Book(0.29, 0.30, 10, 10),
    )

    remaining = bot.positions[PAIR.kalshi_id]

    assert remaining.yes_filled == 1
    assert remaining.no_filled == 1
    assert remaining.contracts == 1
    assert remaining.unmatched_yes == 0
    assert remaining.unmatched_no == 0

    assert remaining.yes_exit_depth_used == 7
    assert remaining.no_exit_depth_used == 2

    # Two matched contracts should have been recorded.
    rows = (tmp_path / "trades.csv").read_text().splitlines()
    assert len(rows) == 2
    assert rows[1].split(",")[4] == "2"

    # Five unmatched contracts should have been liquidated.
    unmatched_rows = (
        tmp_path / "trades_unmatched.csv"
    ).read_text().splitlines()

    assert len(unmatched_rows) == 2
    assert unmatched_rows[1].split(",")[5] == "5"

def test_maximum_open_positions_blocks_new_entries(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    bot.config["max_open_positions"] = 3
    bot.config["max_capital_committed"] = 0.01

    # Simulate three markets that already have open positions.
    bot.positions["market-1"] = object()
    bot.positions["market-2"] = object()
    bot.positions["market-3"] = object()

    # A fourth market must not be allowed to open a position.
    bot.evaluate(
        PAIR,
        Book(0.30, 0.31, 100, 100),
        Book(0.60, 0.61, 100, 100),
    )

    assert len(bot.positions) == 3
    assert PAIR.kalshi_id not in bot.positions

def test_existing_positions_are_managed_at_position_limit(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    bot.config["max_open_positions"] = 3

    pos = make_unmatched_position(
        yes_filled=8,
        no_filled=3,
    )
    pos.contracts = 3

    other_pos_2 = make_unmatched_position()
    other_pos_2.kalshi_id = "market-2"

    other_pos_3 = make_unmatched_position()
    other_pos_3.kalshi_id = "market-3"

    bot.positions[PAIR.kalshi_id] = pos
    bot.positions["market-2"] = other_pos_2
    bot.positions["market-3"] = other_pos_3

    bot.evaluate(
        PAIR,
        Book(0.50, 0.51, 10, 10),
        Book(0.29, 0.30, 10, 10),
    )

    # The existing position was fully managed and closed.
    assert PAIR.kalshi_id not in bot.positions

    # The matched exit and unmatched liquidation were both recorded.
    rows = (tmp_path / "trades.csv").read_text().splitlines()
    assert len(rows) == 2

    unmatched_rows = (
        tmp_path / "trades_unmatched.csv"
    ).read_text().splitlines()
    assert len(unmatched_rows) == 2

def test_capital_committed_includes_fees_and_remaining_fills(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    first = make_unmatched_position(
        yes_filled=8,
        no_filled=3,
    )

    second = make_unmatched_position(
        yes_filled=2,
        no_filled=4,
    )

    bot.positions["market-1"] = first
    bot.positions["market-2"] = second

    # First: (0.40 + 0.01) * 8 + (0.54 + 0.01) * 3 = 4.93
    # Second: (0.40 + 0.01) * 2 + (0.54 + 0.01) * 4 = 3.02
    assert abs(bot._capital_committed() - 7.95) < 0.000001

def test_new_entry_is_blocked_when_capital_limit_would_be_exceeded(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.config["contracts"] = 10
    bot.config["max_capital_committed"] = 5.00

    # Each leg costs 10 contracts, including fees.
    # Total proposed capital: (0.40 + 0.01) * 10
    #                       + (0.54 + 0.01) * 10
    #                       = $9.60
    opportunity = SimpleNamespace(
        yes=SimpleNamespace(
            venue="kalshi",
            price=0.40,
            fee=0.01,
            size=10,
        ),
        no=SimpleNamespace(
            venue="polymarket",
            price=0.54,
            fee=0.01,
            size=10,
        ),
        direction="yes_kalshi_no_polymarket",
        edge=0.02,
    )

    bot.enter(PAIR, opportunity)

    assert PAIR.kalshi_id not in bot.positions
    assert bot._capital_committed() == 0

def test_unmatched_exposure_includes_fees_and_only_unmatched_fills(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    pos = make_unmatched_position(
        yes_filled=8,
        no_filled=3,
    )

    bot.positions[PAIR.kalshi_id] = pos

    # Five unmatched YES contracts at $0.41 each, including fees.
    assert abs(bot._unmatched_exposure() - 2.05) < 0.000001

def test_new_entry_blocked_when_unmatched_exposure_limit_exceeded(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.config["contracts"] = 10
    bot.config["max_capital_committed"] = 100.00
    bot.config["max_unmatched_exposure"] = 5.00

    # An existing position already has five unmatched YES contracts.
    existing = make_unmatched_position(
        yes_filled=8,
        no_filled=3,
    )
    existing.kalshi_id = "market-existing"
    bot.positions["market-existing"] = existing

    opportunity = SimpleNamespace(
        yes=SimpleNamespace(
            venue="kalshi",
            price=0.40,
            fee=0.01,
            size=10,
        ),
        no=SimpleNamespace(
            venue="polymarket",
            price=0.54,
            fee=0.01,
            size=0,
        ),
        direction="yes_kalshi_no_polymarket",
        edge=0.02,
    )

    bot.enter(PAIR, opportunity)

    # Existing exposure: $2.05.
    # Proposed unmatched exposure: $4.10.
    # Combined exposure: $6.15, exceeding the $5 limit.
    assert PAIR.kalshi_id not in bot.positions
    assert abs(bot._unmatched_exposure() - 2.05) < 0.000001

def test_daily_realized_pnl_combines_matched_and_unmatched_trades(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    current_time = datetime.now(timezone.utc)
    today = current_time.strftime("%Y-%m-%d %H:%M:%S UTC")
    yesterday = (
        current_time - timedelta(days=1)
    ).strftime("%Y-%m-%d %H:%M:%S UTC")

    (tmp_path / "trades.csv").write_text(
        "closed_at,pnl_usd\n"
        f"{today},-6.00\n"
        f"{yesterday},-50.00\n"
    )

    (tmp_path / "trades_unmatched.csv").write_text(
        "closed_at,pnl_usd\n"
        f"{today},-4.00\n"
    )

    # Today's matched loss: $6.
    # Today's unmatched loss: $4.
    # Yesterday's $50 loss must not count.
    assert abs(bot._daily_realized_pnl() - (-10.00)) < 0.000001


def test_new_entry_blocked_when_daily_loss_limit_is_reached(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.config["contracts"] = 10
    bot.config["max_daily_loss"] = 10.00

    today = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    (tmp_path / "trades.csv").write_text(
        "closed_at,pnl_usd\n"
        f"{today},-6.00\n"
    )

    (tmp_path / "trades_unmatched.csv").write_text(
        "closed_at,pnl_usd\n"
        f"{today},-4.00\n"
    )

    opportunity = SimpleNamespace(
        yes=SimpleNamespace(
            venue="kalshi",
            price=0.40,
            fee=0.01,
            size=10,
        ),
        no=SimpleNamespace(
            venue="polymarket",
            price=0.54,
            fee=0.01,
            size=10,
        ),
        direction="yes_kalshi_no_polymarket",
        edge=0.02,
    )

    bot.enter(PAIR, opportunity)

    assert PAIR.kalshi_id not in bot.positions


def test_existing_position_still_managed_after_daily_loss_limit(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.config["max_daily_loss"] = 10.00

    today = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    (tmp_path / "trades.csv").write_text(
        "closed_at,pnl_usd\n"
        f"{today},-10.00\n"
    )

    pos = make_unmatched_position()
    bot.positions[PAIR.kalshi_id] = pos

    exit_management_called = False

    def fake_maybe_exit(pair, position, kalshi, poly):
        nonlocal exit_management_called
        exit_management_called = True
        bot.positions.pop(pair.kalshi_id, None)

    bot.maybe_exit = fake_maybe_exit

    bot.evaluate(
        PAIR,
        Book(0.50, 0.51, 10, 10),
        Book(0.29, 0.30, 10, 10),
    )

    # Existing positions must still reach exit management.
    assert exit_management_called
    assert PAIR.kalshi_id not in bot.positions

def test_emergency_stop_blocks_new_entries(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.config["contracts"] = 10
    bot.config["emergency_stop"] = True

    opportunity = SimpleNamespace(
        yes=SimpleNamespace(
            venue="kalshi",
            price=0.40,
            fee=0.01,
            size=10,
        ),
        no=SimpleNamespace(
            venue="polymarket",
            price=0.54,
            fee=0.01,
            size=10,
        ),
        direction="yes_kalshi_no_polymarket",
        edge=0.02,
    )

    # Test the direct entry method.
    bot.enter(PAIR, opportunity)

    assert PAIR.kalshi_id not in bot.positions

    # Test the normal evaluation method.
    bot.evaluate(
        PAIR,
        Book(0.39, 0.40, 500, 300),
        Book(0.46, 0.47, 300, 900),
    )

    assert PAIR.kalshi_id not in bot.positions


def test_emergency_stop_still_manages_existing_positions(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.config["emergency_stop"] = True

    pos = make_unmatched_position()
    bot.positions[PAIR.kalshi_id] = pos

    exit_management_called = False

    def fake_maybe_exit(pair, position, kalshi, poly):
        nonlocal exit_management_called
        exit_management_called = True
        bot.positions.pop(pair.kalshi_id, None)

    bot.maybe_exit = fake_maybe_exit

    bot.evaluate(
        PAIR,
        Book(0.50, 0.51, 10, 10),
        Book(0.29, 0.30, 10, 10),
    )

    # Existing positions must still reach exit management.
    assert exit_management_called
    assert PAIR.kalshi_id not in bot.positions

def test_corrupted_trade_history_returns_unknown_daily_pnl(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    # Simulate a corrupted file containing invalid UTF-8.
    bot.trades_file.write_bytes(b"\xff\xfe\xfa\xff")

    assert bot._daily_realized_pnl() is None


def test_missing_trade_history_columns_returns_unknown_daily_pnl(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    # The required closed_at and pnl_usd columns are missing.
    bot.trades_file.write_text(
        "timestamp,profit\n"
        "2026-11-17,5.00\n",
        encoding="utf-8",
    )

    assert bot._daily_realized_pnl() is None


def test_invalid_daily_pnl_blocks_new_entries(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.config["contracts"] = 10

    today = datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )

    bot.trades_file.write_text(
        "closed_at,pnl_usd\n"
        f"{today},not-a-number\n",
        encoding="utf-8",
    )

    opportunity = SimpleNamespace(
        yes=SimpleNamespace(
            venue="kalshi",
            price=0.40,
            fee=0.01,
            size=10,
        ),
        no=SimpleNamespace(
            venue="polymarket",
            price=0.54,
            fee=0.01,
            size=10,
        ),
        direction="yes_kalshi_no_polymarket",
        edge=0.02,
    )

    bot.enter(PAIR, opportunity)

    assert bot._daily_realized_pnl() is None
    assert bot._daily_loss_block_reason() == (
        "today's realized P&L cannot be verified"
    )
    assert PAIR.kalshi_id not in bot.positions


def test_existing_positions_still_managed_when_trade_history_is_corrupted(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.trades_file.write_bytes(b"\xff\xfe\xfa\xff")

    pos = make_unmatched_position()
    bot.positions[PAIR.kalshi_id] = pos

    exit_management_called = False

    def fake_maybe_exit(pair, position, kalshi, poly):
        nonlocal exit_management_called
        exit_management_called = True
        bot.positions.pop(pair.kalshi_id, None)

    bot.maybe_exit = fake_maybe_exit

    bot.evaluate(
        PAIR,
        Book(0.50, 0.51, 10, 10),
        Book(0.29, 0.30, 10, 10),
    )

    assert exit_management_called
    assert PAIR.kalshi_id not in bot.positions

def test_old_position_format_loads_with_split_fees(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    # Simulate a position saved using the old data format.
    old_state = {
        "kalshi:TEST": {
            "kalshi_id": "kalshi:TEST",
            "direction": "yes_kalshi_no_polymarket",
            "yes_venue": "kalshi",
            "yes_price": 0.40,
            "no_venue": "polymarket",
            "no_price": 0.54,
            "fees": 0.02,
            "contracts": 7,
            "edge": 0.02,
            "opened_at": "2026-11-17 10:00:00 UTC",
        }
    }

    bot.state_file.write_text(
        json.dumps(old_state),
        encoding="utf-8",
    )

    bot.load_state()

    pos = bot.positions["kalshi:TEST"]

    # The old combined fee is split equally between both legs.
    assert abs(pos.yes_fee - 0.01) < 0.000001
    assert abs(pos.no_fee - 0.01) < 0.000001

    # Old positions should restore their original contract quantities.
    assert pos.yes_filled == 7
    assert pos.no_filled == 7
    assert pos.contracts == 7

    assert pos.unmatched_yes == 0
    assert pos.unmatched_no == 0

@pytest.mark.parametrize(
    "rules",
    [
        "insufficient",
        "not_same",
        "unknown",
    ],
)

def test_load_pairs_skips_unsafe_settlement_rules(
    tmp_path, kalshi_fee, poly_fee, monkeypatch, rules
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.pairs.clear()
    bot.pricers.clear()

    bot.config["markets"] = [
        {"event": "e", "kalshi": "kalshi:TEST"}
    ]

    unsafe_pair = SimpleNamespace(
        rules=rules,
        name="Test Event",
        rules_reason="Test settlement-rule comparison",
    )

    monkeypatch.setattr(
        "trading.bot.resolve",
        lambda event_id, kalshi_id: unsafe_pair,
    )

    bot.load_pairs()

    assert bot.pairs == []
    assert "kalshi:TEST" not in bot.pricers

def test_load_pairs_accepts_same_settlement_rules(
    tmp_path, kalshi_fee, poly_fee, monkeypatch
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.pairs.clear()
    bot.pricers.clear()

    bot.config["markets"] = [
        {"event": "e", "kalshi": "kalshi:TEST"}
    ]

    safe_pair = SimpleNamespace(
        rules="same",
        name="Test Event",
        rules_reason="Settlement rules match",
        kalshi_id="kalshi:TEST",
        poly_id="polymarket:TEST",
        flipped=False,
    )

    monkeypatch.setattr(
        "trading.bot.resolve",
        lambda event_id, kalshi_id: safe_pair,
    )

    fee_calls = []

    def fake_fetch_fee_schedule(market_id):
        fee_calls.append(market_id)

        if market_id == "kalshi:TEST":
            return kalshi_fee

        if market_id == "polymarket:TEST":
            return poly_fee

        raise AssertionError(f"Unexpected market ID: {market_id}")

    bot.client.fetch_fee_schedule = fake_fetch_fee_schedule

    bot.load_pairs()

    assert bot.pairs == [safe_pair]
    assert "kalshi:TEST" in bot.pricers
    assert isinstance(bot.pricers["kalshi:TEST"], Pricer)
    assert fee_calls == ["kalshi:TEST", "polymarket:TEST"]

@pytest.mark.parametrize(
    ("rules", "expected_accepted"),
    [
        ("insufficient", True),
        ("not_same", False),
        ("unknown", False),
    ],
)
def test_load_pairs_when_same_rules_not_required_still_rejects_mismatches(
    tmp_path, kalshi_fee, poly_fee, monkeypatch, rules, expected_accepted
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.pairs.clear()
    bot.pricers.clear()

    bot.config["require_same_rules"] = False
    bot.config["markets"] = [
        {"event": "e", "kalshi": "kalshi:TEST"}
    ]

    test_pair = SimpleNamespace(
        rules=rules,
        name="Test Event",
        rules_reason="Test settlement-rule comparison",
        kalshi_id="kalshi:TEST",
        poly_id="polymarket:TEST",
        flipped=False,
    )

    monkeypatch.setattr(
        "trading.bot.resolve",
        lambda event_id, kalshi_id: test_pair,
    )

    fee_calls = []

    def fake_fetch_fee_schedule(market_id):
        fee_calls.append(market_id)

        if market_id == "kalshi:TEST":
            return kalshi_fee

        if market_id == "polymarket:TEST":
            return poly_fee

        raise AssertionError(f"Unexpected market ID: {market_id}")

    bot.client.fetch_fee_schedule = fake_fetch_fee_schedule

    bot.load_pairs()

    if expected_accepted:
        assert bot.pairs == [test_pair]
        assert isinstance(bot.pricers["kalshi:TEST"], Pricer)
        assert fee_calls == ["kalshi:TEST", "polymarket:TEST"]
    else:
        assert bot.pairs == []
        assert "kalshi:TEST" not in bot.pricers
        assert fee_calls == []

def test_save_state_preserves_previous_file_if_replace_fails(
    tmp_path, kalshi_fee, poly_fee, monkeypatch
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    original_state = '{"existing": "valid state"}'

    bot.state_file.write_text(
        original_state,
        encoding="utf-8",
    )

    def fail_replace(source, target):
        raise OSError("Simulated replacement failure")

    monkeypatch.setattr(
        type(bot.state_file),
        "replace",
        fail_replace,
    )

    with pytest.raises(OSError, match="Simulated replacement failure"):
        bot.save_state()

    assert bot.state_file.read_text(encoding="utf-8") == original_state

    temporary_file = bot.state_file.with_name(
        f"{bot.state_file.name}.tmp"
    )

    assert not temporary_file.exists()

def test_record_trade_writes_header_to_existing_empty_file(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.trades_file.touch()

    position = SimpleNamespace(
        opened_at="2026-11-17 10:00:00 UTC",
        direction="kalshi_yes",
        yes_venue="kalshi",
        yes_price=0.40,
        no_venue="polymarket",
        no_price=0.54,
    )

    bot.record_trade(
        PAIR,
        position,
        exit_yes=0.45,
        exit_no=0.54,
        pnl=0.03,
        closed_contracts=2,
    )

    rows = [
        line.split(",")
        for line in bot.trades_file.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    assert len(rows) == 2
    assert len(rows[0]) == 13
    assert len(rows[1]) == 13
    assert rows[0][0] == "opened_at"
    assert rows[0][-1] == "pnl_usd"
    assert rows[1][4] == "2"
    assert float(rows[1][-1]) == 0.06

def test_unmatched_trade_writes_header_to_existing_empty_file(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    bot.unmatched_trades_file.touch()

    pos = make_unmatched_position(yes_filled=5)
    bot.positions[PAIR.kalshi_id] = pos

    bot.liquidate_unmatched(
        PAIR,
        pos,
        Book(0.50, 0.51, 5, 5),
        Book(0.40, 0.41, 5, 5),
    )

    rows = [
        line.split(",")
        for line in bot.unmatched_trades_file.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    assert len(rows) == 2
    assert len(rows[0]) == 12
    assert len(rows[1]) == 12
    assert rows[0][0] == "opened_at"
    assert rows[0][-2] == "pnl_usd"
    assert rows[0][-1] == "transaction_id"
    assert rows[1][3] == "yes"
    assert rows[1][5] == "5"
    assert rows[1][-1]

def test_write_json_atomic_creates_and_replaces_journal(
    tmp_path, kalshi_fee, poly_fee
):
    import json

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    journal = tmp_path / "transactions" / "pending.json"

    bot._write_json_atomic(
        journal,
        {"transaction_id": "test-001", "status": "pending"},
    )

    saved = json.loads(journal.read_text(encoding="utf-8"))

    assert saved["transaction_id"] == "test-001"
    assert saved["status"] == "pending"

    bot._write_json_atomic(
        journal,
        {"transaction_id": "test-002", "status": "completed"},
    )

    saved = json.loads(journal.read_text(encoding="utf-8"))

    assert saved["transaction_id"] == "test-002"
    assert saved["status"] == "completed"

def test_transaction_csv_writer_preserves_legacy_rows_and_prevents_duplicates(
    tmp_path, kalshi_fee, poly_fee
):
    import csv

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    path = bot.trades_file
    path.write_text(
        "closed_at,pnl_usd\n"
        "2026-11-16 10:00:00 UTC,1.25\n",
        encoding="utf-8",
    )

    columns = ["closed_at", "pnl_usd"]

    row = {
        "closed_at": "2026-11-17 10:00:00 UTC",
        "pnl_usd": "2.50",
    }

    bot._write_csv_transaction_once(
        path,
        columns,
        row,
        "test-transaction-001",
    )

    with path.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        saved_rows = list(csv.DictReader(file))

    assert len(saved_rows) == 2
    assert saved_rows[0]["pnl_usd"] == "1.25"
    assert saved_rows[0]["transaction_id"] == ""
    assert saved_rows[1]["pnl_usd"] == "2.50"
    assert saved_rows[1]["transaction_id"] == "test-transaction-001"

    original_content = path.read_text(encoding="utf-8")

    bot._write_csv_transaction_once(
        path,
        columns,
        {"closed_at": "different timestamp", "pnl_usd": "999.00"},
        "test-transaction-001",
    )

    assert path.read_text(encoding="utf-8") == original_content

def test_recovery_finishes_pending_transaction_without_duplicate_trade(
    tmp_path, kalshi_fee, poly_fee
):
    import csv
    import json
    from dataclasses import asdict

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    position = make_unmatched_position(yes_filled=5)
    bot.positions[PAIR.kalshi_id] = position

    transaction_id = "recovery-test-001"

    columns = ["closed_at", "pnl_usd"]

    row = {
        "closed_at": "2026-11-17 10:30:00 UTC",
        "pnl_usd": "2.50",
    }

    # Simulate the trade having been written before the crash.
    bot._write_csv_transaction_once(
        bot.trades_file,
        columns,
        row,
        transaction_id,
    )

    transaction = {
        "transaction_id": transaction_id,
        "ledger": "matched",
        "columns": columns,
        "row": row,
        "positions": {
            key: asdict(value)
            for key, value in bot.positions.items()
        },
        "event": {
            "event_type": "paper_exit",
            "market": PAIR.name,
            "details": {"total_pnl": 2.50},
        },
    }

    bot._write_json_atomic(
        bot.transaction_file,
        transaction,
    )

    # Recover the interrupted transaction.
    bot.load_state()

    with bot.trades_file.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        saved_trades = list(csv.DictReader(file))

    assert len(saved_trades) == 1
    assert saved_trades[0]["transaction_id"] == transaction_id

    saved_positions = json.loads(
        bot.state_file.read_text(encoding="utf-8")
    )

    assert PAIR.kalshi_id in saved_positions

    saved_events = [
        json.loads(line)
        for line in bot.events.path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    assert len(saved_events) == 1
    assert saved_events[0]["details"]["transaction_id"] == transaction_id

    assert not bot.transaction_file.exists()

def test_matched_exit_commits_transaction_and_clears_journal(
    tmp_path, kalshi_fee, poly_fee
):
    import csv
    import json

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    # Open a paper position.
    bot.evaluate(
        PAIR,
        Book(0.39, 0.40, 500, 300),
        Book(0.46, 0.47, 300, 900),
    )

    assert PAIR.kalshi_id in bot.positions

    # Allow the position to reach its exit target.
    bot.evaluate(
        PAIR,
        Book(0.41, 0.42, 500, 500),
        Book(0.46, 0.47, 300, 900),
    )

    bot.evaluate(
        PAIR,
        Book(0.48, 0.49, 500, 500),
        Book(0.44, 0.45, 500, 500),
    )

    assert PAIR.kalshi_id not in bot.positions

    with bot.trades_file.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        trades = list(csv.DictReader(file))

    assert len(trades) == 1

    transaction_id = trades[0]["transaction_id"]
    assert transaction_id

    events = [
        json.loads(line)
        for line in bot.events.path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    exit_events = [
        event
        for event in events
        if event["event_type"] == "paper_exit"
    ]

    assert len(exit_events) == 1
    assert exit_events[0]["details"]["transaction_id"] == transaction_id

    saved_positions = json.loads(
        bot.state_file.read_text(encoding="utf-8")
    )

    assert PAIR.kalshi_id not in saved_positions
    assert not bot.transaction_file.exists()

def test_interrupted_matched_exit_recovers_after_restart(
    tmp_path, kalshi_fee, poly_fee, monkeypatch
):
    import csv
    import json

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    # Open a paper position.
    bot.evaluate(
        PAIR,
        Book(0.39, 0.40, 500, 300),
        Book(0.46, 0.47, 300, 900),
    )

    # First update: hold the position.
    bot.evaluate(
        PAIR,
        Book(0.41, 0.42, 500, 500),
        Book(0.46, 0.47, 300, 900),
    )

    def simulate_crash_before_trade_write(
        path, columns, row, transaction_id
    ):
        raise OSError("Simulated interruption before CSV write")

    monkeypatch.setattr(
        bot,
        "_write_csv_transaction_once",
        simulate_crash_before_trade_write,
    )

    # Second update: trigger the exit, then simulate interruption.
    with pytest.raises(
        OSError,
        match="Simulated interruption before CSV write",
    ):
        bot.evaluate(
            PAIR,
            Book(0.48, 0.49, 500, 500),
            Book(0.44, 0.45, 500, 500),
        )

    assert bot.transaction_file.exists()

    # Simulate restarting the bot.
    recovered = make_bot(tmp_path, kalshi_fee, poly_fee)
    recovered.load_state()

    # The completed trade must appear exactly once.
    with recovered.trades_file.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        trades = list(csv.DictReader(file))

    assert len(trades) == 1
    assert trades[0]["transaction_id"]

    # The closed position must not reappear.
    assert PAIR.kalshi_id not in recovered.positions

    # The exit event must reference the same transaction.
    events = [
        json.loads(line)
        for line in recovered.events.path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    exit_events = [
        event
        for event in events
        if event["event_type"] == "paper_exit"
    ]

    assert len(exit_events) == 1
    assert (
        exit_events[0]["details"]["transaction_id"]
        == trades[0]["transaction_id"]
    )

    assert not recovered.transaction_file.exists()

def test_interrupted_unmatched_exit_recovers_after_restart(
    tmp_path, kalshi_fee, poly_fee, monkeypatch
):
    import csv
    import json

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    pos = make_unmatched_position(yes_filled=5)
    bot.positions[PAIR.kalshi_id] = pos

    # Save the original open position before simulating the crash.
    bot.save_state()

    def simulate_crash_before_trade_write(
        path, columns, row, transaction_id
    ):
        raise OSError("Simulated interruption before CSV write")

    monkeypatch.setattr(
        bot,
        "_write_csv_transaction_once",
        simulate_crash_before_trade_write,
    )

    # Attempt to liquidate the unmatched YES exposure.
    with pytest.raises(
        OSError,
        match="Simulated interruption before CSV write",
    ):
        bot.liquidate_unmatched(
            PAIR,
            pos,
            Book(0.50, 0.51, 5, 5),
            Book(0.40, 0.41, 5, 5),
        )

    assert bot.transaction_file.exists()

    # Simulate restarting the bot.
    recovered = make_bot(tmp_path, kalshi_fee, poly_fee)
    recovered.load_state()

    # The unmatched trade must be recorded exactly once.
    with recovered.unmatched_trades_file.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        trades = list(csv.DictReader(file))

    assert len(trades) == 1
    transaction_id = trades[0]["transaction_id"]
    assert transaction_id

    assert trades[0]["leg"] == "yes"
    assert trades[0]["contracts"] == "5"

    # The fully liquidated position must not return.
    assert PAIR.kalshi_id not in recovered.positions

    # The event must use the same transaction ID.
    events = [
        json.loads(line)
        for line in recovered.events.path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    exit_events = [
        event
        for event in events
        if event["event_type"] == "unmatched_exit"
    ]

    assert len(exit_events) == 1
    assert exit_events[0]["details"]["transaction_id"] == transaction_id

    assert not recovered.transaction_file.exists()

def test_recovery_after_csv_write_but_before_state_save(
    tmp_path, kalshi_fee, poly_fee, monkeypatch
):
    import csv
    import json
    from dataclasses import asdict

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    # Preserve an existing open position as the pre-crash state.
    position = make_unmatched_position(yes_filled=5)
    bot.positions[PAIR.kalshi_id] = position
    bot.save_state()

    transaction_id = "recovery-after-csv-001"

    columns = [
        "opened_at",
        "closed_at",
        "market",
        "leg",
        "venue",
        "contracts",
        "entry_price",
        "exit_price",
        "entry_fee_per_contract",
        "exit_fee_per_contract",
        "pnl_usd",
    ]

    row = {
        "opened_at": position.opened_at,
        "closed_at": "2026-11-17 10:30:00 UTC",
        "market": PAIR.name,
        "leg": "yes",
        "venue": "kalshi",
        "contracts": 5,
        "entry_price": 0.40,
        "exit_price": 0.50,
        "entry_fee_per_contract": 0.01,
        "exit_fee_per_contract": 0.01,
        "pnl_usd": 0.36,
    }

    transaction = {
        "transaction_id": transaction_id,
        "ledger": "unmatched",
        "columns": columns,
        "row": row,
        "positions": {},
        "event": {
            "event_type": "unmatched_exit",
            "market": PAIR.name,
            "details": {
                "leg": "yes",
                "contracts_liquidated": 5,
                "pnl": 0.36,
            },
        },
    }

    # Create the pending journal before simulating the crash.
    bot._write_json_atomic(
        bot.transaction_file,
        transaction,
    )

    def fail_state_write(path, data):
        raise OSError("Simulated state-save failure")

    # The CSV write succeeds, but the subsequent state write fails.
    monkeypatch.setattr(
        bot,
        "_write_json_atomic",
        fail_state_write,
    )

    with pytest.raises(OSError, match="Simulated state-save failure"):
        bot._recover_pending_transaction()

    assert bot.transaction_file.exists()

    with bot.unmatched_trades_file.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        first_attempt = list(csv.DictReader(file))

    assert len(first_attempt) == 1
    assert first_attempt[0]["transaction_id"] == transaction_id

    # Restart: recovery should deduplicate the CSV record and finish.
    recovered = make_bot(tmp_path, kalshi_fee, poly_fee)
    recovered.load_state()

    with recovered.unmatched_trades_file.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        final_trades = list(csv.DictReader(file))

    assert len(final_trades) == 1
    assert final_trades[0]["transaction_id"] == transaction_id

    assert recovered.positions == {}

    events = [
        json.loads(line)
        for line in recovered.events.path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    exit_events = [
        event
        for event in events
        if event["event_type"] == "unmatched_exit"
    ]

    assert len(exit_events) == 1
    assert exit_events[0]["details"]["transaction_id"] == transaction_id

    assert not recovered.transaction_file.exists()

def test_recovery_after_event_log_failure(
    tmp_path, kalshi_fee, poly_fee, monkeypatch
):
    import csv
    import json

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    transaction_id = "recovery-event-failure-001"

    columns = [
        "opened_at",
        "closed_at",
        "market",
        "leg",
        "venue",
        "contracts",
        "entry_price",
        "exit_price",
        "entry_fee_per_contract",
        "exit_fee_per_contract",
        "pnl_usd",
    ]

    row = {
        "opened_at": "2026-11-17 10:00:00 UTC",
        "closed_at": "2026-11-17 10:30:00 UTC",
        "market": PAIR.name,
        "leg": "yes",
        "venue": "kalshi",
        "contracts": 5,
        "entry_price": 0.40,
        "exit_price": 0.50,
        "entry_fee_per_contract": 0.01,
        "exit_fee_per_contract": 0.01,
        "pnl_usd": 0.36,
    }

    transaction = {
        "transaction_id": transaction_id,
        "ledger": "unmatched",
        "columns": columns,
        "row": row,
        "positions": {},
        "event": {
            "event_type": "unmatched_exit",
            "market": PAIR.name,
            "details": {
                "leg": "yes",
                "contracts_liquidated": 5,
                "pnl": 0.36,
            },
        },
    }

    bot._write_json_atomic(
        bot.transaction_file,
        transaction,
    )

    def fail_event_log(event_type, **kwargs):
        raise OSError("Simulated event-log failure")

    monkeypatch.setattr(
        bot.events,
        "log",
        fail_event_log,
    )

    with pytest.raises(OSError, match="Simulated event-log failure"):
        bot._recover_pending_transaction()

    assert bot.transaction_file.exists()

    # The CSV was committed before the event failed.
    with bot.unmatched_trades_file.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        first_attempt = list(csv.DictReader(file))

    assert len(first_attempt) == 1
    assert first_attempt[0]["transaction_id"] == transaction_id

    # Restart and allow normal event logging.
    recovered = make_bot(tmp_path, kalshi_fee, poly_fee)
    recovered.load_state()

    # The existing CSV record must not be duplicated.
    with recovered.unmatched_trades_file.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        final_trades = list(csv.DictReader(file))

    assert len(final_trades) == 1

    # Exactly one exit event should now exist.
    events = [
        json.loads(line)
        for line in recovered.events.path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    assert len(events) == 1
    assert events[0]["details"]["transaction_id"] == transaction_id

    assert not recovered.transaction_file.exists()

def test_transaction_writer_migrates_legacy_windows_encoded_csv(
    tmp_path, kalshi_fee, poly_fee
):
    import csv

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    legacy_content = (
        "closed_at,pnl_usd,market\n"
        "2026-11-17 10:00:00 UTC,1.25,Test Event · Outcome\n"
    )

    bot.trades_file.write_bytes(
        legacy_content.encode("cp1252")
    )

    bot._write_csv_transaction_once(
        bot.trades_file,
        ["closed_at", "pnl_usd"],
        {
            "closed_at": "2026-11-17 11:00:00 UTC",
            "pnl_usd": "2.50",
            "market": "Second Event · Outcome",
        },
        "legacy-migration-001",
    )

    with bot.trades_file.open(
        "r",
        newline="",
        encoding="utf-8",
    ) as file:
        rows = list(csv.DictReader(file))

    assert len(rows) == 2
    assert rows[0]["market"] == "Test Event · Outcome"
    assert rows[0]["pnl_usd"] == "1.25"
    assert rows[1]["market"] == "Second Event · Outcome"
    assert rows[1]["transaction_id"] == "legacy-migration-001"

@pytest.mark.parametrize(
    "setting,value",
    [
        ("contracts", 0),
        ("contracts", 2.5),
        ("contracts", True),
        ("max_open_positions", 0),
        ("max_open_positions", 1.5),
        ("max_open_positions", True),
        ("max_capital_committed", 0),
        ("max_capital_committed", -10),
        ("max_capital_committed", float("nan")),
        ("max_capital_committed", float("inf")),
        ("max_unmatched_exposure", -1),
        ("max_unmatched_exposure", float("nan")),
        ("max_daily_loss", 0),
        ("max_daily_loss", float("inf")),
        ("kalshi_poll_seconds", 0),
        ("kalshi_poll_seconds", -1),
        ("kalshi_poll_seconds", float("nan")),
        ("status_interval_seconds", -1),
        ("status_interval_seconds", float("inf")),
    ],
)
def test_bot_rejects_invalid_safety_configuration(
    tmp_path, kalshi_fee, poly_fee, setting, value
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    config = bot.config.copy()
    config[setting] = value

    with pytest.raises(ValueError, match=setting):
        ArbitrageBot(
            config,
            client=SimpleNamespace(),
        )


def test_bot_accepts_zero_unmatched_limit_and_status_interval(
    tmp_path, kalshi_fee, poly_fee
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    config = bot.config.copy()

    config["max_unmatched_exposure"] = 0
    config["status_interval_seconds"] = 0

    validated_bot = ArbitrageBot(
        config,
        client=SimpleNamespace(),
    )

    assert validated_bot.config["max_unmatched_exposure"] == 0
    assert validated_bot.status_every == 0

def test_save_state_preserves_previous_file_if_replace_fails(
    tmp_path, kalshi_fee, poly_fee, monkeypatch
):
    bot = make_bot(tmp_path, kalshi_fee, poly_fee)

    original_state = '{"existing": "valid state"}'

    bot.state_file.write_text(
        original_state,
        encoding="utf-8",
    )

    def fail_replace(source, target):
        raise OSError("Simulated replacement failure")

    monkeypatch.setattr(
        type(bot.state_file),
        "replace",
        fail_replace,
    )

    with pytest.raises(OSError, match="Simulated replacement failure"):
        bot.save_state()

    assert bot.state_file.read_text(encoding="utf-8") == original_state

    temporary_file = bot.state_file.with_name(
        f"{bot.state_file.name}.tmp"
    )

    assert not temporary_file.exists()

def test_start_closes_feed_when_update_loop_fails(
    tmp_path, kalshi_fee, poly_fee
):
    import asyncio
    import json

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    bot.load_pairs = lambda: setattr(bot, "pairs", [PAIR])
    bot.load_state = lambda: None

    class FakeFeed:
        kalshi_mode = "test"

        def __init__(self):
            self.started = False
            self.closed = False

        async def start(self):
            self.started = True

        async def close(self):
            self.closed = True

    feed = FakeFeed()

    async def failing_run(feed):
        raise RuntimeError("Simulated update-loop failure")

    bot._run = failing_run

    async def scenario():
        with pytest.raises(
            RuntimeError,
            match="Simulated update-loop failure",
        ):
            await asyncio.wait_for(
                bot.start(feed=feed),
                timeout=2,
            )

    asyncio.run(scenario())

    assert feed.started
    assert feed.closed

    events = [
        json.loads(line)
        for line in bot.events.path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    event_types = [event["event_type"] for event in events]

    assert "session_start" in event_types
    assert "session_stop" in event_types

def test_start_logs_session_stop_when_feed_close_fails(
    tmp_path, kalshi_fee, poly_fee
):
    import asyncio
    import json

    bot = make_bot(tmp_path, kalshi_fee, poly_fee)
    bot.load_pairs = lambda: setattr(bot, "pairs", [PAIR])
    bot.load_state = lambda: None

    class FakeFeed:
        kalshi_mode = "test"

        def __init__(self):
            self.started = asyncio.Event()
            self.closed = False

        async def start(self):
            self.started.set()

        async def close(self):
            self.closed = True
            raise RuntimeError("Simulated feed close failure")

    feed = FakeFeed()

    async def idle_run(feed):
        await asyncio.Event().wait()

    bot._run = idle_run

    async def scenario():
        task = asyncio.create_task(bot.start(feed=feed))

        await asyncio.wait_for(feed.started.wait(), timeout=2)

        # Allow the update loop and stop waiter to start.
        await asyncio.sleep(0)

        bot.stop()

        with pytest.raises(
            RuntimeError,
            match="Simulated feed close failure",
        ):
            await task

    asyncio.run(scenario())

    assert feed.closed

    events = [
        json.loads(line)
        for line in bot.events.path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]

    event_types = [event["event_type"] for event in events]

    assert "session_start" in event_types
    assert "session_stop" in event_types