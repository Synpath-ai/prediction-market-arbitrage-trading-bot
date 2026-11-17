import argparse
import pytest

from backtest.backtest import (
    simulate,
    _positive_contracts,
    _poly_spread,
)
from backtest.data import poly_history
from src.arbitrage import Book, Pricer
from src.strategy import Strategy

H = 3_600_000


def rows(*quotes):
    """(kalshi bid, kalshi ask, polymarket price) per hour."""
    return [(i * H, Book(kb, ka), Book(p, p)) for i, (kb, ka, p) in enumerate(quotes)]


def test_enters_on_the_edge_and_sells_at_the_target(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)
    trades = simulate(rows(
        (0.44, 0.45, 0.45),
        (0.39, 0.40, 0.46),
        (0.41, 0.42, 0.45),
        (0.47, 0.48, 0.45),
        (0.44, 0.45, 0.45),
    ), pricer, Strategy(0.02, 0.02))

    assert len(trades) == 1
    t = trades[0]
    assert (t["entry_ms"], t["exit_ms"], t["direction"]) == (
        1 * H, 3 * H, "kalshi_yes"
    )
    assert t["pnl"] >= 0.02


def test_never_sells_at_a_loss_and_reports_what_is_still_held(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)
    trades = simulate(rows(
        (0.39, 0.40, 0.46),
        (0.35, 0.36, 0.46),
        (0.36, 0.37, 0.46),
    ), pricer, Strategy(0.02, 0.02))

    assert len(trades) == 1 and trades[0]["exit_ms"] is None
    assert trades[0]["pnl"] == trades[0]["edge"] > 0.02
    assert trades[0]["mark"] < 0


def test_polymarket_spread_creates_bid_ask_prices(monkeypatch):
    from types import SimpleNamespace
    import backtest.data as data

    class FakePolymarket:
        def fetch_ohlcv(self, *args, **kwargs):
            return [SimpleNamespace(timestamp=0, close=0.50)]

    monkeypatch.setattr(data.synpath, "Polymarket", FakePolymarket)

    book = poly_history(
        "test-market", 0, H, spread=0.02
    )[0]

    assert book.bid == 0.49
    assert book.ask == 0.51


def test_zero_spread_keeps_midpoint_unchanged(monkeypatch):
    from types import SimpleNamespace
    import backtest.data as data

    class FakePolymarket:
        def fetch_ohlcv(self, *args, **kwargs):
            return [SimpleNamespace(timestamp=0, close=0.50)]

    monkeypatch.setattr(data.synpath, "Polymarket", FakePolymarket)

    book = poly_history(
        "test-market", 0, H, spread=0
    )[0]

    assert book.bid == 0.50
    assert book.ask == 0.50


def test_invalid_spread_is_rejected(monkeypatch):
    import backtest.data as data

    class FakePolymarket:
        def fetch_ohlcv(self, *args, **kwargs):
            return []

    monkeypatch.setattr(data.synpath, "Polymarket", FakePolymarket)

    import pytest

    with pytest.raises(ValueError):
        poly_history("test-market", 0, H, spread=1.5)


def test_history_does_not_carry_forward_missing_polymarket_bars(monkeypatch):
    from types import SimpleNamespace
    import backtest.data as data

    pair = SimpleNamespace(
        kalshi_id="kalshi:test",
        poly_id="polymarket:test",
        flipped=False,
    )

    monkeypatch.setattr(
        data,
        "kalshi_history",
        lambda *args, **kwargs: {
            0: Book(0.40, 0.41),
            H: Book(0.40, 0.41),
        },
    )

    monkeypatch.setattr(
        data,
        "poly_history",
        lambda *args, **kwargs: {
            0: Book(0.50, 0.50),
        },
    )

    result = data.history(pair, 0, 2 * H, timeframe="1h")

    assert [row[0] for row in result] == [0]


def test_history_rejects_stale_kalshi_quotes(monkeypatch):
    from types import SimpleNamespace
    import backtest.data as data

    pair = SimpleNamespace(
        kalshi_id="kalshi:test",
        poly_id="polymarket:test",
        flipped=False,
    )

    monkeypatch.setattr(
        data,
        "kalshi_history",
        lambda *args, **kwargs: {
            0: Book(0.40, 0.41),
        },
    )

    monkeypatch.setattr(
        data,
        "poly_history",
        lambda *args, **kwargs: {
            6 * H: Book(0.50, 0.50),
        },
    )

    result = data.history(pair, 0, 7 * H, timeframe="1h")

    assert result == []

@pytest.mark.parametrize(
    "value",
    ["0", "-1", "1.5", "nan", "inf", "-inf", "invalid"],
)
def test_backtest_rejects_invalid_contract_quantities(value):
    with pytest.raises(argparse.ArgumentTypeError):
        _positive_contracts(value)


def test_backtest_accepts_valid_contract_quantities():
    assert _positive_contracts("1") == 1
    assert _positive_contracts("3.0") == 3


@pytest.mark.parametrize(
    "value",
    ["-0.01", "1.01", "nan", "inf", "-inf", "invalid"],
)
def test_backtest_rejects_invalid_poly_spreads(value):
    with pytest.raises(argparse.ArgumentTypeError):
        _poly_spread(value)


def test_backtest_accepts_valid_poly_spreads():
    assert _poly_spread("0") == 0
    assert _poly_spread("0.02") == 0.02
    assert _poly_spread("1") == 1


@pytest.mark.parametrize(
    "spread",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.01,
        1.01,
        True,
    ],
)
def test_poly_history_rejects_invalid_spreads(spread):
    with pytest.raises(ValueError):
        poly_history(
            "test-market",
            0,
            H,
            spread=spread,
        )