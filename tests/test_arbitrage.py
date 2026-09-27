import pytest

from src.arbitrage import Book, Pricer, per_contract_fee
from src.strategy import Strategy


def test_no_side_is_the_same_book_read_from_the_other_side():
    b = Book(0.40, 0.42, bid_size=100, ask_size=50)
    assert b.no_ask == 0.60 and b.no_bid == 0.58
    f = b.flipped()
    assert (f.bid, f.ask, f.bid_size, f.ask_size) == (0.58, 0.60, 50, 100)


def test_fees_follow_each_venue_schedule(kalshi_fee, poly_fee):
    # Kalshi: 0.07 * C * P * (1 - P), rounded up to the cent for the order
    assert per_contract_fee(kalshi_fee, 0.5, 1000) == pytest.approx(0.0175)
    assert per_contract_fee(kalshi_fee, 0.5, 1) == pytest.approx(0.02)       # 1.75c rounds up to 2c
    # Polymarket: 0.04 * P * (1 - P)
    assert per_contract_fee(poly_fee, 0.5, 1000) == pytest.approx(0.01)


def test_both_combinations_are_priced_best_first(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)
    kalshi = Book(0.39, 0.40, bid_size=500, ask_size=800)   # Kalshi YES is cheap
    poly = Book(0.46, 0.47, bid_size=300, ask_size=900)     # Polymarket NO costs 1 - 0.46 = 0.54
    best, other = pricer.opportunities(kalshi, poly)
    assert best.direction == "kalshi_yes"
    assert (best.yes.venue, best.yes.price, best.no.venue, best.no.price) == ("kalshi", 0.40, "polymarket", 0.54)
    assert best.cost == pytest.approx(0.94)
    assert best.edge == pytest.approx(1 - 0.94 - best.fees, abs=1e-4)
    assert best.size == 300                                 # the NO leg's depth is Polymarket's YES bid size
    assert other.edge < 0


def test_books_that_disagree_by_more_than_half_are_a_mismatch(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee)
    assert pricer.opportunities(Book(0.10, 0.11), Book(0.80, 0.81)) == []


def test_unwind_is_the_round_trip_after_both_fees(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)
    opp = pricer.opportunities(Book(0.39, 0.40), Book(0.46, 0.47))[0]
    # Later the venues agree at 45/46: sell Kalshi YES at 0.45, Polymarket NO at 1 - 0.46 = 0.54.
    later_k, later_p = Book(0.45, 0.46), Book(0.45, 0.46)
    exit_fees = per_contract_fee(kalshi_fee, 0.45, 1000) + per_contract_fee(poly_fee, 0.54, 1000)
    expected = 0.45 + 0.54 - opp.cost - opp.fees - exit_fees
    assert pricer.unwind(opp, later_k, later_p) == pytest.approx(expected, abs=1e-4)


def test_strategy_enters_at_the_edge_and_sells_only_at_the_target(kalshi_fee, poly_fee):
    s = Strategy(entry_edge=0.02, take_profit=0.02)
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)
    wide = pricer.opportunities(Book(0.39, 0.40), Book(0.46, 0.47))
    narrow = pricer.opportunities(Book(0.44, 0.45), Book(0.45, 0.46))
    assert s.entry(wide) is wide[0]
    assert s.entry(narrow) is None
    assert s.should_exit(0.021) and s.should_exit(0.02)
    assert not s.should_exit(0.019) and not s.should_exit(-0.05) and not s.should_exit(None)
