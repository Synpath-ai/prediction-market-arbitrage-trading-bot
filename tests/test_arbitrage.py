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

def test_live_opportunities_require_known_order_book_depth(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=10)

    # Price-only books remain supported for historical backtesting.
    kalshi = Book(0.39, 0.40)
    poly = Book(0.46, 0.47)

    assert pricer.opportunities(kalshi, poly)
    assert pricer.opportunities(
        kalshi,
        poly,
        require_depth=True,
    ) == []

    # Known quantities allow strict live pricing.
    kalshi = Book(0.39, 0.40, bid_size=20, ask_size=15)
    poly = Book(0.46, 0.47, bid_size=12, ask_size=18)

    opportunities = pricer.opportunities(
        kalshi,
        poly,
        require_depth=True,
    )

    assert opportunities
    assert sorted(opp.size for opp in opportunities) == [12, 18]

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


def test_opportunity_fees_use_actual_available_quantity(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)

    book = Book(0.49, 0.50, bid_size=1, ask_size=1)
    opportunities = pricer.opportunities(book, book)

    assert len(opportunities) == 2

    for opp in opportunities:
        assert opp.size == 1

        kalshi_leg = (
            opp.yes if opp.yes.venue == "kalshi" else opp.no
        )

        # A one-contract Kalshi order at 50 cents incurs a 2-cent fee.
        assert kalshi_leg.fee == pytest.approx(0.02)


def test_unwind_uses_actual_position_quantity(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)

    opp = pricer.opportunities(
        Book(0.39, 0.40),
        Book(0.46, 0.47),
    )[0]

    later_k = Book(0.45, 0.46)
    later_p = Book(0.45, 0.46)

    result = pricer.unwind(
        opp, later_k, later_p, contracts=1
    )

    exit_fees = (
        per_contract_fee(kalshi_fee, 0.45, 1)
        + per_contract_fee(poly_fee, 0.54, 1)
    )

    expected = (
        0.45 + 0.54 - opp.cost - opp.fees - exit_fees
    )

    assert result == pytest.approx(expected, abs=1e-4)

def test_unwind_defaults_to_opportunity_matched_quantity(
    kalshi_fee, poly_fee
):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)

    opp = pricer.opportunities(
        Book(0.39, 0.40, bid_size=1, ask_size=1),
        Book(0.46, 0.47, bid_size=1, ask_size=1),
    )[0]

    assert opp.size == 1

    result = pricer.unwind(
        opp,
        Book(0.45, 0.46),
        Book(0.45, 0.46),
    )

    exit_fees = (
        per_contract_fee(kalshi_fee, 0.45, 1)
        + per_contract_fee(poly_fee, 0.54, 1)
    )

    expected = (
        0.45 + 0.54 - opp.cost - opp.fees - exit_fees
    )

    assert result == pytest.approx(expected, abs=1e-4)

def test_unwind_defaults_to_opportunity_matched_quantity(
    kalshi_fee, poly_fee
):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)

    opp = pricer.opportunities(
        Book(0.39, 0.40, bid_size=1, ask_size=1),
        Book(0.46, 0.47, bid_size=1, ask_size=1),
    )[0]

    assert opp.size == 1

    result = pricer.unwind(
        opp,
        Book(0.45, 0.46),
        Book(0.45, 0.46),
    )

    exit_fees = (
        per_contract_fee(kalshi_fee, 0.45, 1)
        + per_contract_fee(poly_fee, 0.54, 1)
    )

    expected = (
        0.45 + 0.54 - opp.cost - opp.fees - exit_fees
    )

    assert result == pytest.approx(expected, abs=1e-4)

def test_unknown_fee_schedule_skips_opportunities(kalshi_fee):
    from synpath import FeeSchedule

    unknown_fee = FeeSchedule(
        venue="polymarket",
        scope="market",
        scope_id="unknown",
        fee_type="unknown",
    )

    pricer = Pricer(kalshi_fee, unknown_fee, contracts=10)

    opportunities = pricer.opportunities(
        Book(0.39, 0.40),
        Book(0.46, 0.47),
    )

    assert opportunities == []


def test_unknown_exit_fee_returns_none(kalshi_fee):
    from synpath import FeeSchedule

    unknown_fee = FeeSchedule(
        venue="polymarket",
        scope="market",
        scope_id="unknown",
        fee_type="unknown",
    )

    pricer = Pricer(kalshi_fee, unknown_fee, contracts=10)

    opp = pricer.opportunities(
        Book(0.39, 0.40),
        Book(0.46, 0.47),
    )

    # No entry opportunity should be created with an unknown fee.
    assert opp == []


def test_unwind_returns_none_when_exit_fee_is_unknown(kalshi_fee, poly_fee):
    from synpath import FeeSchedule

    pricer = Pricer(kalshi_fee, poly_fee, contracts=10)

    opp = pricer.opportunities(
        Book(0.39, 0.40),
        Book(0.46, 0.47),
    )[0]

    unknown_fee = FeeSchedule(
        venue="polymarket",
        scope="market",
        scope_id="unknown",
        fee_type="unknown",
    )

    # Simulate the fee schedule becoming unavailable before exit.
    pricer.fee["polymarket"] = unknown_fee

    result = pricer.unwind(
        opp,
        Book(0.45, 0.46),
        Book(0.45, 0.46),
        contracts=10,
    )

    assert result is None

def test_opportunity_fees_use_smaller_matched_quantity(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=1000)

    kalshi = Book(0.49, 0.50, bid_size=1, ask_size=10)
    poly = Book(0.49, 0.50, bid_size=1, ask_size=100)

    opportunities = pricer.opportunities(kalshi, poly)

    assert len(opportunities) == 2

    for opp in opportunities:
        assert opp.size == 1

        kalshi_leg = (
            opp.yes if opp.yes.venue == "kalshi" else opp.no
        )

        # Both fee calculations should use the matched quantity of 1.
        assert kalshi_leg.fee == pytest.approx(0.02)

@pytest.mark.parametrize(
    "contracts",
    [
        0,
        -1,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
    ],
)
def test_pricer_rejects_invalid_contract_configuration(
    kalshi_fee, poly_fee, contracts
):
    with pytest.raises(ValueError):
        Pricer(kalshi_fee, poly_fee, contracts=contracts)


@pytest.mark.parametrize(
    "invalid_book",
    [
        Book(float("nan"), 0.40, 10, 10),
        Book(0.39, float("inf"), 10, 10),
        Book(-0.01, 0.40, 10, 10),
        Book(0.39, 1.01, 10, 10),
        Book(0.39, 0.40, float("nan"), 10),
        Book(0.39, 0.40, 10, float("inf")),
        Book(0.39, 0.40, -1, 10),
        Book(0.39, 0.40, 10, -1),
    ],
)
def test_pricer_rejects_invalid_order_books(
    kalshi_fee, poly_fee, invalid_book
):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=10)
    valid_book = Book(0.39, 0.40, 100, 100)

    assert pricer.opportunities(invalid_book, valid_book) == []
    assert pricer.opportunities(valid_book, invalid_book) == []


@pytest.mark.parametrize(
    "price",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.01,
        1.01,
    ],
)
def test_per_contract_fee_rejects_invalid_prices(
    kalshi_fee, price
):
    with pytest.raises(ValueError):
        per_contract_fee(kalshi_fee, price, 10)


@pytest.mark.parametrize(
    "contracts",
    [
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        -1,
    ],
)
def test_per_contract_fee_rejects_invalid_quantities(
    kalshi_fee, contracts
):
    with pytest.raises(ValueError):
        per_contract_fee(kalshi_fee, 0.50, contracts)


def test_per_contract_fee_rejects_non_finite_fee_estimate():
    from types import SimpleNamespace

    invalid_schedule = SimpleNamespace(
        estimate=lambda price, contracts: float("nan")
    )

    with pytest.raises(ValueError):
        per_contract_fee(invalid_schedule, 0.50, 10)

@pytest.mark.parametrize(
    "crossed_book",
    [
        Book(0.45, 0.44, 10, 10),
        Book(0.60, 0.40, 100, 100),
    ],
)
def test_pricer_rejects_crossed_order_books(
    kalshi_fee, poly_fee, crossed_book
):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=10)

    valid_book = Book(0.39, 0.40, 100, 100)

    assert pricer.opportunities(crossed_book, valid_book) == []
    assert pricer.opportunities(valid_book, crossed_book) == []

def test_pricer_accepts_locked_order_books(kalshi_fee, poly_fee):
    pricer = Pricer(kalshi_fee, poly_fee, contracts=10)

    locked_book = Book(
        0.40,
        0.40,
        bid_size=10,
        ask_size=10,
    )

    opportunities = pricer.opportunities(
        locked_book,
        locked_book,
    )

    assert len(opportunities) == 2