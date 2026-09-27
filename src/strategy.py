"""The strategy, shared by the live bot and the backtest so both trade the same rule.

  Entry  buy YES on one venue and NO on the other when, after taker fees, the pair costs at least
         `entry_edge` less than the $1 it pays out.
  Exit   sell both legs only when the round trip nets at least `take_profit` after fees on both
         sides. Otherwise hold: the pair pays exactly $1 at resolution, so a position that never
         reaches the target still makes the edge it locked at entry. It never sells at a loss.

One position per market at a time.
"""
from __future__ import annotations

from dataclasses import dataclass

from .arbitrage import Opportunity


@dataclass(frozen=True)
class Strategy:
    entry_edge: float = 0.02
    take_profit: float = 0.02

    def entry(self, opportunities: list[Opportunity]) -> Opportunity | None:
        """The combination to buy now, or `None`."""
        best = opportunities[0] if opportunities else None
        return best if best is not None and best.edge >= self.entry_edge else None

    def should_exit(self, round_trip: float | None) -> bool:
        """Sell both legs now? Only when selling already makes the target."""
        return round_trip is not None and round_trip >= self.take_profit

    def describe(self) -> str:
        return (f"enter when both sides lock ≥ {self.entry_edge * 100:g}¢ after fees  ·  "
                f"sell only when the round trip nets ≥ {self.take_profit * 100:g}¢, otherwise hold")
