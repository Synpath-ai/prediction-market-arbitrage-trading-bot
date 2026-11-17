


"""Paper execution and unmatched-position accounting.

This module sends no real orders.
"""
from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class FillResult:
    requested: int
    yes_filled: int
    no_filled: int

    @property
    def matched(self) -> int:
        return min(self.yes_filled, self.no_filled)

    @property
    def unmatched_yes(self) -> int:
        return self.yes_filled - self.matched

    @property
    def unmatched_no(self) -> int:
        return self.no_filled - self.matched


@dataclass(frozen=True)
class LiquidationResult:
    requested: int
    liquidated: int
    remaining: int
    pnl: float | None


def simulate_fills(
    requested: int,
    yes_available: int,
    no_available: int,
) -> FillResult:
    """Simulate independent fills limited by available depth."""
    if any(
        not isinstance(q, int) or isinstance(q, bool) or q < 0
        for q in (requested, yes_available, no_available)
    ):
        raise ValueError("Quantities must be non-negative integers")

    return FillResult(
        requested=requested,
        yes_filled=min(requested, yes_available),
        no_filled=min(requested, no_available),
    )


def _validate_leg(price: float, fee: float | None) -> None:
    """Reject invalid, non-finite prices and fees."""
    try:
        valid_price = (
            not isinstance(price, bool)
            and isfinite(price)
            and 0 <= price <= 1
        )
    except (TypeError, ValueError, OverflowError):
        valid_price = False

    if not valid_price:
        raise ValueError("Prices must be finite numbers between 0 and 1")

    if fee is not None:
        try:
            valid_fee = (
                not isinstance(fee, bool)
                and isfinite(fee)
                and fee >= 0
            )
        except (TypeError, ValueError, OverflowError):
            valid_fee = False

        if not valid_fee:
            raise ValueError("Fees must be finite, non-negative numbers")


def unmatched_exit_pnl(
    quantity: int,
    entry_price: float,
    entry_fee: float | None,
    exit_price: float,
    exit_fee: float | None,
) -> float | None:
    """Calculate P&L for closing one unmatched leg."""
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
        raise ValueError("Quantity must be a non-negative integer")

    _validate_leg(entry_price, entry_fee)
    _validate_leg(exit_price, exit_fee)

    if quantity == 0:
        return 0.0

    if entry_fee is None or exit_fee is None:
        return None

    return round(
        quantity * (exit_price - entry_price - entry_fee - exit_fee),
        4,
    )


def simulate_liquidation(
    quantity: int,
    available_exit_depth: int,
    entry_price: float,
    entry_fee: float | None,
    exit_price: float,
    exit_fee: float | None,
) -> LiquidationResult:
    """Simulate liquidation limited by available exit-book depth."""
    if any(
        not isinstance(q, int) or isinstance(q, bool) or q < 0
        for q in (quantity, available_exit_depth)
    ):
        raise ValueError("Quantities must be non-negative integers")

    liquidated = min(quantity, available_exit_depth)
    remaining = quantity - liquidated

    pnl = unmatched_exit_pnl(
        liquidated,
        entry_price,
        entry_fee,
        exit_price,
        exit_fee,
    )

    return LiquidationResult(quantity, liquidated, remaining, pnl)


def simulate_multi_level_liquidation(
    quantity: int,
    exit_levels: list[tuple[float, int]],
    entry_price: float,
    entry_fee: float | None,
    exit_fee: float | None,
) -> LiquidationResult:
    """Sell across bid levels ordered from highest price to lowest."""
    if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
        raise ValueError("Quantity must be a non-negative integer")

    _validate_leg(entry_price, entry_fee)

    previous_price = float("inf")

    for price, size in exit_levels:
        _validate_leg(price, exit_fee)

        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("Depth must be a non-negative integer")

        if price > previous_price:
            raise ValueError("Exit levels must be ordered highest to lowest")

        previous_price = price

    remaining = quantity
    liquidated = 0
    proceeds = 0.0

    for price, size in exit_levels:
        if remaining == 0:
            break

        filled = min(remaining, size)
        proceeds += filled * price
        liquidated += filled
        remaining -= filled

    if liquidated == 0:
        pnl = 0.0
    elif entry_fee is None or exit_fee is None:
        pnl = None
    else:
        pnl = round(
            proceeds
            - liquidated * entry_price
            - liquidated * entry_fee
            - liquidated * exit_fee,
            4,
        )

    return LiquidationResult(quantity, liquidated, remaining, pnl)
