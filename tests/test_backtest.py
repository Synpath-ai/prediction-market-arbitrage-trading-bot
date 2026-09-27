from backtest.backtest import simulate
from src.arbitrage import Book, Pricer
from src.strategy import Strategy

H = 3_600_000


def rows(*quotes):
    """(kalshi bid, kalshi ask, polymarket price) per hour."""
    return [(i * H, Book(kb, ka), Book(p, p)) for i, (kb, ka, p) in enumerate(quotes)]


def test_enters_on_the_edge_and_sells_at_the_target(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)
    trades = simulate(rows(
        (0.44, 0.45, 0.45),   # no gap
        (0.39, 0.40, 0.46),   # Kalshi 6c cheap: enter YES Kalshi 0.40 + NO Poly 0.54
        (0.41, 0.42, 0.45),   # narrower, but the round trip is below 2c: hold
        (0.47, 0.48, 0.45),   # Kalshi overshoots: sell
        (0.44, 0.45, 0.45),
    ), pricer, Strategy(0.02, 0.02))
    assert len(trades) == 1
    t = trades[0]
    assert (t["entry_ms"], t["exit_ms"], t["direction"]) == (1 * H, 3 * H, "kalshi_yes")
    assert t["pnl"] >= 0.02


def test_never_sells_at_a_loss_and_reports_what_is_still_held(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)
    trades = simulate(rows(
        (0.39, 0.40, 0.46),   # enter
        (0.35, 0.36, 0.46),   # the gap widens: selling would lose, so hold
        (0.36, 0.37, 0.46),
    ), pricer, Strategy(0.02, 0.02))
    assert len(trades) == 1 and trades[0]["exit_ms"] is None
    assert trades[0]["pnl"] == trades[0]["edge"] > 0.02     # held to resolution, it pays the locked edge
    assert trades[0]["mark"] < 0                            # while selling now would have lost
