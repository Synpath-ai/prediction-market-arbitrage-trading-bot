import pytest

from src.strategy import Strategy


@pytest.mark.parametrize(
    "field,value",
    [
        ("entry_edge", -0.01),
        ("entry_edge", float("nan")),
        ("entry_edge", float("inf")),
        ("entry_edge", float("-inf")),
        ("entry_edge", True),
        ("entry_edge", "0.02"),
        ("take_profit", -0.01),
        ("take_profit", float("nan")),
        ("take_profit", float("inf")),
        ("take_profit", float("-inf")),
        ("take_profit", True),
        ("take_profit", "0.02"),
    ],
)
def test_strategy_rejects_invalid_thresholds(field, value):
    with pytest.raises(ValueError, match=field):
        Strategy(**{field: value})


def test_strategy_accepts_zero_thresholds():
    strategy = Strategy(
        entry_edge=0,
        take_profit=0,
    )

    assert strategy.entry_edge == 0
    assert strategy.take_profit == 0


def test_strategy_accepts_default_thresholds():
    strategy = Strategy()

    assert strategy.entry_edge == 0.02
    assert strategy.take_profit == 0.02


def test_strategy_never_exits_at_a_loss():
    strategy = Strategy(
        entry_edge=0.02,
        take_profit=0.02,
    )

    assert strategy.should_exit(-0.01) is False
    assert strategy.should_exit(0.01) is False
    assert strategy.should_exit(0.02) is True