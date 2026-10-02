"""Pricing one matched pair: what buying both sides costs, what it locks, and what selling it back
would make.

Both venues quote one book per market in the YES price, read from both sides:

  YES ask   the book's ask                 NO ask   1 - the book's bid
  YES bid   the book's bid                 NO bid   1 - the book's ask

Two combinations pay exactly $1 whatever happens: YES on Kalshi + NO on Polymarket, and YES on
Polymarket + NO on Kalshi. Each is priced at the prices a taker actually gets (asks to buy, bids
to sell) plus each venue's taker fee from its published schedule.
"""
from __future__ import annotations

from dataclasses import dataclass

from synpath import FeeSchedule

# Two books quoting the same proposition do not sit on opposite sides of 50%. When they appear to,
# the pairing is wrong (a mislabelled side, the wrong market), and the "profit" is the mistake.
MAX_DISAGREEMENT = 0.5

VENUES = ("kalshi", "polymarket")


@dataclass(frozen=True)
class Book:
    """Top of one venue's book in the YES price, with the size quoted at each. `None` where the
    venue quotes nothing (or, for sizes, where the data has none, as in candle history)."""
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
        """The same book seen from the other side, for a venue that states the proposition the
        other way round (its YES is our NO)."""
        return Book(self.no_bid, self.no_ask, self.ask_size, self.bid_size)


def per_contract_fee(schedule: FeeSchedule, price: float, contracts: float) -> float:
    """Taker fee per contract for an order of `contracts` at `price`. Kalshi rounds the fee of
    each order up to the cent, so the size matters."""
    if price <= 0 or price >= 1:
        return 0.0
    fee = schedule.estimate(price, contracts)
    return 0.0 if fee is None else fee / contracts


@dataclass(frozen=True)
class Leg:
    venue: str        # "kalshi" or "polymarket"
    side: str         # "yes" or "no"
    price: float      # paid per contract
    fee: float        # taker fee per contract
    size: float | None = None   # contracts quoted at that price, where known


@dataclass(frozen=True)
class Opportunity:
    direction: str    # "kalshi_yes": YES on Kalshi + NO on Polymarket; "poly_yes": the reverse
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
        """Profit per pair if held to resolution: $1 minus what both legs and their fees cost."""
        return round(1.0 - self.cost - self.fees, 4)

    @property
    def size(self) -> float | None:
        """Contracts both legs can be bought at these prices, where the books say."""
        sizes = [s for s in (self.yes.size, self.no.size) if s is not None]
        return min(sizes) if sizes else None


class Pricer:
    def __init__(self, kalshi_fee: FeeSchedule, poly_fee: FeeSchedule, *, contracts: float = 1000):
        self.fee = {"kalshi": kalshi_fee, "polymarket": poly_fee}
        self.contracts = contracts

    def leg(self, venue: str, side: str, price: float, size: float | None = None) -> Leg:
        return Leg(venue, side, price, per_contract_fee(self.fee[venue], price, self.contracts), size)

    def opportunities(self, kalshi: Book, poly: Book) -> list[Opportunity]:
        """Both combinations that can be priced, best edge first. Empty when the books disagree
        about the proposition by more than MAX_DISAGREEMENT."""
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
            out.append(Opportunity(direction,
                                   self.leg(yes_venue, "yes", yes_book.ask, yes_book.ask_size),
                                   self.leg(no_venue, "no", no_book.no_ask, no_book.bid_size)))
        return sorted(out, key=lambda o: -o.edge)

    def exit_prices(self, opp: Opportunity, kalshi: Book, poly: Book) -> tuple[float, float] | None:
        """What selling each leg fetches now: the YES leg at its venue's YES bid, the NO leg at its
        venue's NO bid. `None` when either side has no bid."""
        books = {"kalshi": kalshi, "polymarket": poly}
        yes_bid, no_bid = books[opp.yes.venue].bid, books[opp.no.venue].no_bid
        if yes_bid is None or no_bid is None:
            return None
        return yes_bid, no_bid

    def unwind(self, opp: Opportunity, kalshi: Book, poly: Book) -> float | None:
        """Profit per pair if both legs were sold now: the round trip after entry and exit fees."""
        prices = self.exit_prices(opp, kalshi, poly)
        if prices is None:
            return None
        yes_bid, no_bid = prices
        exit_fees = self.leg(opp.yes.venue, "yes", yes_bid).fee + self.leg(opp.no.venue, "no", no_bid).fee
        return round(yes_bid + no_bid - opp.cost - opp.fees - exit_fees, 4)
