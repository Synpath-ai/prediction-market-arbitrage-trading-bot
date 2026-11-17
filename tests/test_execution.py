
import pytest

from trading.execution import simulate_fills


def test_both_legs_fill_completely():
    result = simulate_fills(10, 10, 10)

    assert result.matched == 10
    assert result.unmatched_yes == 0
    assert result.unmatched_no == 0


def test_unequal_partial_fills():
    result = simulate_fills(10, 10, 6)

    assert result.yes_filled == 10
    assert result.no_filled == 6
    assert result.matched == 6
    assert result.unmatched_yes == 4
    assert result.unmatched_no == 0


def test_one_leg_does_not_fill():
    result = simulate_fills(10, 0, 10)

    assert result.matched == 0
    assert result.unmatched_yes == 0
    assert result.unmatched_no == 10


def test_fills_cannot_exceed_requested_quantity():
    result = simulate_fills(5, 20, 20)

    assert result.yes_filled == 5
    assert result.no_filled == 5


@pytest.mark.parametrize(
    "requested,yes,no",
    [
        (-1, 10, 10),
        (10, -1, 10),
        (10, 10, -1),
    ],
)
def test_negative_quantities_are_rejected(requested, yes, no):
    with pytest.raises(ValueError):
        simulate_fills(requested, yes, no)


def test_unmatched_yes_position_liquidation():
    from trading.execution import unmatched_exit_pnl

    pnl = unmatched_exit_pnl(
        quantity=4,
        entry_price=0.40,
        entry_fee=0.01,
        exit_price=0.45,
        exit_fee=0.01,
    )

    assert pnl == pytest.approx(0.12)


def test_unmatched_position_can_have_a_loss():
    from trading.execution import unmatched_exit_pnl

    pnl = unmatched_exit_pnl(
        quantity=4,
        entry_price=0.50,
        entry_fee=0.01,
        exit_price=0.45,
        exit_fee=0.01,
    )

    assert pnl == pytest.approx(-0.28)


def test_unknown_exit_fee_returns_none():
    from trading.execution import unmatched_exit_pnl

    pnl = unmatched_exit_pnl(
        quantity=4,
        entry_price=0.40,
        entry_fee=0.01,
        exit_price=0.45,
        exit_fee=None,
    )

    assert pnl is None


def test_zero_quantity_has_zero_pnl():
    from trading.execution import unmatched_exit_pnl

    pnl = unmatched_exit_pnl(
        quantity=0,
        entry_price=0.40,
        entry_fee=0.01,
        exit_price=0.45,
        exit_fee=0.01,
    )

    assert pnl == 0.0


def test_liquidation_respects_exit_depth():
    from trading.execution import simulate_liquidation

    result = simulate_liquidation(
        quantity=10,
        available_exit_depth=6,
        entry_price=0.40,
        entry_fee=0.01,
        exit_price=0.45,
        exit_fee=0.01,
    )

    assert result.requested == 10
    assert result.liquidated == 6
    assert result.remaining == 4
    assert result.pnl == pytest.approx(0.18)


def test_liquidation_with_no_exit_depth():
    from trading.execution import simulate_liquidation

    result = simulate_liquidation(
        quantity=10,
        available_exit_depth=0,
        entry_price=0.40,
        entry_fee=0.01,
        exit_price=0.45,
        exit_fee=0.01,
    )

    assert result.liquidated == 0
    assert result.remaining == 10
    assert result.pnl == 0.0


def test_liquidation_with_unknown_fee():
    from trading.execution import simulate_liquidation

    result = simulate_liquidation(
        quantity=10,
        available_exit_depth=5,
        entry_price=0.40,
        entry_fee=0.01,
        exit_price=0.45,
        exit_fee=None,
    )

    assert result.liquidated == 5
    assert result.remaining == 5
    assert result.pnl is None


def test_multi_level_liquidation_uses_multiple_prices():
    from trading.execution import simulate_multi_level_liquidation

    result = simulate_multi_level_liquidation(
        quantity=10,
        exit_levels=[(0.45, 5), (0.44, 5)],
        entry_price=0.40,
        entry_fee=0.01,
        exit_fee=0.01,
    )

    assert result.liquidated == 10
    assert result.remaining == 0
    assert result.pnl == pytest.approx(0.25)


def test_multi_level_liquidation_leaves_unfilled_quantity():
    from trading.execution import simulate_multi_level_liquidation

    result = simulate_multi_level_liquidation(
        quantity=10,
        exit_levels=[(0.45, 3), (0.44, 2)],
        entry_price=0.40,
        entry_fee=0.01,
        exit_fee=0.01,
    )

    assert result.liquidated == 5
    assert result.remaining == 5
    assert result.pnl == pytest.approx(0.13)


def test_multi_level_liquidation_rejects_negative_depth():
    from trading.execution import simulate_multi_level_liquidation

    with pytest.raises(ValueError):
        simulate_multi_level_liquidation(
            quantity=10,
            exit_levels=[(0.45, -5)],
            entry_price=0.40,
            entry_fee=0.01,
            exit_fee=0.01,
        )


@pytest.mark.parametrize(
    "requested,yes,no",
    [
        (1.5, 10, 10),
        (10, 2.5, 10),
        (10, 10, 3.5),
        (True, 10, 10),
    ],
)
def test_fills_reject_non_integer_quantities(requested, yes, no):
    with pytest.raises(ValueError):
        simulate_fills(requested, yes, no)


def test_liquidation_rejects_invalid_quantity():
    from trading.execution import simulate_liquidation

    with pytest.raises(ValueError):
        simulate_liquidation(
            quantity=2.5,
            available_exit_depth=5,
            entry_price=0.40,
            entry_fee=0.01,
            exit_price=0.45,
            exit_fee=0.01,
        )


def test_liquidation_rejects_negative_exit_depth():
    from trading.execution import simulate_liquidation

    with pytest.raises(ValueError):
        simulate_liquidation(
            quantity=5,
            available_exit_depth=-1,
            entry_price=0.40,
            entry_fee=0.01,
            exit_price=0.45,
            exit_fee=0.01,
        )


@pytest.mark.parametrize(
    "entry_price,exit_price,entry_fee,exit_fee",
    [
        (-0.01, 0.45, 0.01, 0.01),
        (0.40, 1.01, 0.01, 0.01),
        (0.40, 0.45, -0.01, 0.01),
        (0.40, 0.45, 0.01, -0.01),
    ],
)
def test_unmatched_exit_rejects_invalid_prices_and_fees(
    entry_price, exit_price, entry_fee, exit_fee
):
    from trading.execution import unmatched_exit_pnl

    with pytest.raises(ValueError):
        unmatched_exit_pnl(
            quantity=5,
            entry_price=entry_price,
            entry_fee=entry_fee,
            exit_price=exit_price,
            exit_fee=exit_fee,
        )


def test_multi_level_liquidation_rejects_incorrect_price_order():
    from trading.execution import simulate_multi_level_liquidation

    with pytest.raises(ValueError):
        simulate_multi_level_liquidation(
            quantity=10,
            exit_levels=[(0.40, 5), (0.45, 5)],
            entry_price=0.35,
            entry_fee=0.01,
            exit_fee=0.01,
        )


def test_multi_level_liquidation_with_no_available_depth():
    from trading.execution import simulate_multi_level_liquidation

    result = simulate_multi_level_liquidation(
        quantity=10,
        exit_levels=[],
        entry_price=0.40,
        entry_fee=0.01,
        exit_fee=0.01,
    )

    assert result.liquidated == 0
    assert result.remaining == 10
    assert result.pnl == 0.0

@pytest.mark.parametrize(
    "entry_price,exit_price,entry_fee,exit_fee",
    [
        (float("nan"), 0.45, 0.01, 0.01),
        (float("inf"), 0.45, 0.01, 0.01),
        (0.40, float("-inf"), 0.01, 0.01),
        (0.40, 0.45, float("nan"), 0.01),
        (0.40, 0.45, float("inf"), 0.01),
        (0.40, 0.45, 0.01, float("-inf")),
    ],
)
def test_unmatched_exit_rejects_non_finite_prices_and_fees(
    entry_price, exit_price, entry_fee, exit_fee
):
    from trading.execution import unmatched_exit_pnl

    with pytest.raises(ValueError):
        unmatched_exit_pnl(
            quantity=5,
            entry_price=entry_price,
            entry_fee=entry_fee,
            exit_price=exit_price,
            exit_fee=exit_fee,
        )

@pytest.mark.parametrize(
    "exit_levels,entry_fee,exit_fee",
    [
        ([(float("nan"), 5)], 0.01, 0.01),
        ([(float("inf"), 5)], 0.01, 0.01),
        ([(0.45, 5)], float("nan"), 0.01),
        ([(0.45, 5)], 0.01, float("inf")),
    ],
)
def test_multi_level_liquidation_rejects_non_finite_values(
    exit_levels, entry_fee, exit_fee
):
    from trading.execution import simulate_multi_level_liquidation

    with pytest.raises(ValueError):
        simulate_multi_level_liquidation(
            quantity=5,
            exit_levels=exit_levels,
            entry_price=0.40,
            entry_fee=entry_fee,
            exit_fee=exit_fee,
        )