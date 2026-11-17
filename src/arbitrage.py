
"""Pricing one matched pair and calculating entry and exit costs."""
from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from synpath import FeeSchedule

MAX_DISAGREEMENT = 0.5

VENUES = ("kalshi", "polymarket")
def _is_finite_number(value: object) -> bool:
    """Check that a value is a finite numeric value, excluding booleans."""
    try:
        return not isinstance(value, bool) and isfinite(value)
    except (TypeError, ValueError, OverflowError):
        return False


def _valid_price(value: float) -> bool:
    """Check that a probability price is finite and between zero and one."""
    return (
        _is_finite_number(value)
        and 0 <= value <= 1
    )

@dataclass(frozen=True)
class Book:
    """Top of one venue's YES book, including displayed quantities."""
    bid: float | None
    ask: float | None
    bid_size: float | None = None
    ask_size: float | None = None

    @property
    def no_ask(self) -> float | None:
        return None if self.bid is None else round(1.0 - self.bid, 4)

    @property
    def no_bid(self) -> float | None:
        return None if self.ask is None else round(1.0 - self.ask, 4)

    @property
    def mid(self) -> float | None:
        if self.bid is not None and self.ask is not None:
            return (self.bid + self.ask) / 2
        return self.ask if self.ask is not None else self.bid

    def flipped(self) -> "Book":
        """View the same proposition from the opposite YES/NO perspective."""
        return Book(self.no_bid, self.no_ask, self.ask_size, self.bid_size)

def _valid_book(book: Book) -> bool:
    """Check that all available prices and displayed quantities are valid."""
    return (
        (book.bid is None or _valid_price(book.bid))
        and (book.ask is None or _valid_price(book.ask))
        and (
            book.bid is None
            or book.ask is None
            or book.bid <= book.ask
        )
        and (
            book.bid_size is None
            or (
                _is_finite_number(book.bid_size)
                and book.bid_size >= 0
            )
        )
        and (
            book.ask_size is None
            or (
                _is_finite_number(book.ask_size)
                and book.ask_size >= 0
            )
        )
    )

def per_contract_fee(
    schedule: FeeSchedule,
    price: float,
    contracts: float,
) -> float | None:
    """Return taker fee per contract, or None if the fee is unknown."""
    if not _valid_price(price):
        raise ValueError("Price must be finite and between 0 and 1")

    if not _is_finite_number(contracts):
        raise ValueError("Contract quantity must be finite")

    if contracts < 0:
        raise ValueError("Contract quantity cannot be negative")

    if contracts == 0:
        return None

    if price == 0 or price == 1:
        return 0.0

    fee = schedule.estimate(price, contracts)

    if fee is None:
        return None

    if not _is_finite_number(fee) or fee < 0:
        raise ValueError("Fee estimate must be finite and non-negative")

    return fee / contracts


@dataclass(frozen=True)
class Leg:
    venue: str
    side: str
    price: float
    fee: float
    size: float | None = None


@dataclass(frozen=True)
class Opportunity:
    direction: str
    yes: Leg
    no: Leg

    @property
    def cost(self) -> float:
        return self.yes.price + self.no.price

    @property
    def fees(self) -> float:
        return self.yes.fee + self.no.fee

    @property
    def edge(self) -> float:
        """Estimated profit per matched pair if held to resolution."""
        return round(1.0 - self.cost - self.fees, 4)

    @property
    def size(self) -> float | None:
        """Smaller independently available entry depth, when known."""
        sizes = [s for s in (self.yes.size, self.no.size) if s is not None]
        return min(sizes) if sizes else None


class Pricer:
    def __init__(
        self,
        kalshi_fee: FeeSchedule,
        poly_fee: FeeSchedule,
        *,
        contracts: float = 1000,
    ):
        if not _is_finite_number(contracts) or contracts <= 0:
            raise ValueError(
                "Configured contracts must be a finite positive number"
            )

        self.fee = {
            "kalshi": kalshi_fee,
            "polymarket": poly_fee,
        }
        self.contracts = contracts

    def leg(
        self,
        venue: str,
        side: str,
        price: float,
        size: float | None = None,
        contracts: float | None = None,
    ) -> Leg | None:
        quantity = self.contracts if contracts is None else contracts
        fee = per_contract_fee(self.fee[venue], price, quantity)

        if fee is None:
            return None

        return Leg(venue, side, price, fee, size)


    def opportunities(
        self,
        kalshi: Book,
        poly: Book,
        *,
        require_depth: bool = False,
    ) -> list[Opportunity]:
        """Price both combinations using available top-of-book depth."""
        if not _valid_book(kalshi) or not _valid_book(poly):
            return []

        k, p = kalshi.mid, poly.mid

        if k is not None and p is not None and abs(k - p) > MAX_DISAGREEMENT:
            return []

        out = []

        for direction, yes_venue, yes_book, no_venue, no_book in (
            ("kalshi_yes", "kalshi", kalshi, "polymarket", poly),
            ("poly_yes", "polymarket", poly, "kalshi", kalshi),
        ):
            if yes_book.ask is None or no_book.no_ask is None:
                continue

            # Live pricing must have known quantities on both required sides.
            if require_depth and (
                yes_book.ask_size is None or no_book.bid_size is None
            ):
                continue

            # Use known quantities when available. Historical data may have none.
            matched_quantity = self.contracts

            if yes_book.ask_size is not None:
                matched_quantity = min(matched_quantity, yes_book.ask_size)

            if no_book.bid_size is not None:
                matched_quantity = min(matched_quantity, no_book.bid_size)

            if matched_quantity <= 0:
                continue

            yes_leg = self.leg(
                yes_venue,
                "yes",
                yes_book.ask,
                yes_book.ask_size,
                contracts=matched_quantity,
            )

            no_leg = self.leg(
                no_venue,
                "no",
                no_book.no_ask,
                no_book.bid_size,
                contracts=matched_quantity,
            )

            if yes_leg is None or no_leg is None:
                continue

            out.append(Opportunity(direction, yes_leg, no_leg))

        return sorted(out, key=lambda opportunity: opportunity.edge, reverse=True)


    def exit_prices(
        self,
        opp: Opportunity,
        kalshi: Book,
        poly: Book,
    ) -> tuple[float, float] | None:
        """Return the current YES and NO exit bids."""
        books = {"kalshi": kalshi, "polymarket": poly}

        yes_bid = books[opp.yes.venue].bid
        no_bid = books[opp.no.venue].no_bid

        if yes_bid is None or no_bid is None:
            return None

        return yes_bid, no_bid

    def unwind(
        self,
        opp: Opportunity,
        kalshi: Book,
        poly: Book,
        contracts: float | None = None,
    ) -> float | None:
        """Estimated round-trip profit per pair after entry and exit fees."""
        prices = self.exit_prices(opp, kalshi, poly)

        if prices is None:
            return None

        if contracts is None:
            quantity = self.contracts
            if opp.size is not None:
                quantity = min(quantity, opp.size)
        else:
            quantity = contracts

        yes_bid, no_bid = prices

        yes_exit = self.leg(
            opp.yes.venue,
            "yes",
            yes_bid,
            contracts=quantity,
        )

        no_exit = self.leg(
            opp.no.venue,
            "no",
            no_bid,
            contracts=quantity,
        )

        if yes_exit is None or no_exit is None:
            return None

        exit_fees = yes_exit.fee + no_exit.fee

        return round(
            yes_bid + no_bid - opp.cost - opp.fees - exit_fees,
            4,
        )