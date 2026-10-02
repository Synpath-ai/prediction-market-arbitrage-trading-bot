"""Which Polymarket listing is the same market as a Kalshi one, and whether they settle alike.

No name matching: Synpath's matching service (`synpath.match_market`) pairs listings that parse to
the same proposition, and says whether Polymarket states it the same way round. The catalog's
event page then says how the two venues' rules compare, dimension by dimension:

  same          every settlement dimension it checks agrees
  insufficient  it could not confirm one (a detail missing from one venue's rules)
  not_same      a dimension differs: the two can settle differently, so YES + NO is not $1

Only a pair that settles alike is an arbitrage; the others are a bet on how the rules differ.
"""
from __future__ import annotations

from dataclasses import dataclass

import httpx
import synpath

CATALOG = "https://api.synpath.dev"


@dataclass(frozen=True)
class Pair:
    event_id: str
    event: str          # the catalog event title
    outcome: str        # the outcome this pair prices
    kalshi_id: str      # "kalshi:<TICKER>"
    poly_id: str        # "polymarket:<market id>"
    flipped: bool       # Polymarket's YES pays when this proposition is false
    rules: str          # "same", "insufficient", "not_same" or "unknown"
    rules_reason: str | None = None

    @property
    def name(self) -> str:
        return f"{self.event} · {self.outcome}"


def resolve(event_id: str, kalshi_id: str, *, http: httpx.Client | None = None) -> Pair:
    """The pair for one Kalshi listing on a catalog event."""
    link = synpath.match_market(kalshi_id, venue="polymarket").matched
    if link is None or link.venue != "polymarket":
        raise LookupError(f"{kalshi_id}: Polymarket lists no market asking the same question")
    own = http is None
    http = http or httpx.Client(timeout=30)
    try:
        event = http.get(f"{CATALOG}/events/{event_id}").raise_for_status().json()
    finally:
        if own:
            http.close()
    native = kalshi_id.split(":", 1)[1]
    for outcome in event["outcomes"]:
        kalshi = next((c for c in outcome["contracts"] if c["venue"] == "kalshi" and c["native_market_id"] == native), None)
        if kalshi is None:
            continue
        poly = next((c for c in outcome["contracts"] if f"polymarket:{c['native_market_id']}" == link.id), None)
        comparison = next((c for c in outcome.get("comparisons") or [] if poly and
                           {c["left_contract_id"], c["right_contract_id"]} == {kalshi["contract_id"], poly["contract_id"]}), None)
        return Pair(event_id, event["title"].strip(), outcome["label"], kalshi_id, link.id,
                    link.side_map.get("yes") == "no",
                    comparison["internal_state"] if comparison else "unknown",
                    comparison["reason"] if comparison else None)
    raise LookupError(f"{kalshi_id} is not on event {event_id}")


def tradeable(pair: Pair, *, require_same: bool = True) -> bool:
    """Whether the two listings settle alike enough for YES + NO to pay $1."""
    return pair.rules == "same" or (not require_same and pair.rules == "insufficient")
