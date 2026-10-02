"""Find markets to trade: Kalshi listings that Polymarket lists as the same proposition, with how
the two venues' rules compare and where both venues price them now.

    python -m src.discover                         # the 10 markets with the best profit right now
    python -m src.discover --query "senate"        # events matching a search
    python -m src.discover --sort volume --limit 5 # the most traded instead
    python -m src.discover --save                  # and use them

Prints the top --limit pairs (10 by default) in --sort order. With --save, those whose rules
match are written to markets.json,
which the bot and the backtest use when config.py lists no markets.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import httpx
import synpath

from config import MARKETS_FILE

from .arbitrage import Book, Pricer
from .matcher import CATALOG, Pair, resolve


def search(http: httpx.Client, *, query: str | None, domain: str | None, events: int) -> list[dict]:
    """Live events listed on both venues, most traded first."""
    found, cursor = [], None
    while len(found) < events:
        params = {"matched_only": "true", "sort": "relevance" if query else "volume",
                  "limit": min(100, events - len(found))}
        if query:
            params["q"] = query
        if domain:
            params["domain"] = domain
        if cursor:
            params["cursor"] = cursor
        page = http.get(f"{CATALOG}/search", params=params).raise_for_status().json()
        found += page["results"]
        cursor = page.get("next_cursor")
        if not cursor or not page["results"]:
            break
    return found[:events]


@dataclass
class Candidate:
    pair: Pair
    volume: float                        # both platforms' traded volume, as the catalog reports it
    price: float | None                  # Kalshi's last YES price, as the catalog reports it
    kalshi: Book | None = None           # live books, read for --sort gap/edge or --quotes
    poly: Book | None = None
    edge: float | None = None            # best locked profit per $1 pair after fees, right now

    @property
    def gap(self) -> float | None:
        if self.kalshi is None or self.poly is None or self.kalshi.mid is None or self.poly.mid is None:
            return None
        return abs(self.kalshi.mid - self.poly.mid)


def event_pairs(http: httpx.Client, event_id: str) -> list[Candidate]:
    detail = http.get(f"{CATALOG}/events/{event_id}").raise_for_status().json()
    out = []
    for outcome in detail["outcomes"]:
        kalshi = next((c for c in outcome["contracts"] if c["venue"] == "kalshi" and c.get("primary")), None)
        if kalshi is None:
            continue
        try:
            pair = resolve(event_id, f"kalshi:{kalshi['native_market_id']}", http=http)
        except LookupError:
            continue                     # no Polymarket listing asks the same question
        poly = next((c for c in outcome["contracts"] if f"polymarket:{c['native_market_id']}" == pair.poly_id), {})
        yes = next((s for s in kalshi.get("selections") or [] if str(s.get("native_id")).lower() == "yes"), {})
        out.append(Candidate(pair, float(kalshi.get("volume") or 0) + float(poly.get("volume") or 0), yes.get("price")))
    return out


def read_books(client: synpath.Client, c: Candidate) -> Candidate:
    """Both platforms' live top of book, and the best edge after fees they offer now."""
    def top(market_id: str) -> Book | None:
        try:
            b = client.fetch_order_book(market_id, side="yes", depth=1)
        except Exception:
            return None
        return Book(b.best_bid.price if b.best_bid else None, b.best_ask.price if b.best_ask else None)
    c.kalshi, poly = top(c.pair.kalshi_id), top(c.pair.poly_id)
    c.poly = poly.flipped() if poly is not None and c.pair.flipped else poly
    if c.kalshi is not None and c.poly is not None:
        try:
            pricer = Pricer(client.fetch_fee_schedule(c.pair.kalshi_id), client.fetch_fee_schedule(c.pair.poly_id))
            opps = pricer.opportunities(c.kalshi, c.poly)
            c.edge = opps[0].edge if opps else None
        except Exception:
            c.edge = None
    return c


def cents(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}¢"


SORTS = {
    "volume": lambda c: -c.volume,
    "gap": lambda c: -(c.gap if c.gap is not None else -1),
    "edge": lambda c: -(c.edge if c.edge is not None else -1),
}


def main() -> None:
    ap = argparse.ArgumentParser(description="List Kalshi/Polymarket pairs of the same market")
    ap.add_argument("--query", help="search text")
    ap.add_argument("--domain", help="election, sports, econ, fed, crypto, weather, ...")
    ap.add_argument("--events", type=int, default=30, help="events to look through")
    ap.add_argument("--limit", type=int, default=10, help="markets to list and save (default 10)")
    ap.add_argument("--sort", choices=tuple(SORTS), default="edge",
                    help="edge: best locked profit after fees right now (default); gap: largest price gap "
                         "between the platforms now; volume: most traded first (fastest, no live books)")
    ap.add_argument("--min-price", type=float, default=0.05,
                    help="skip markets priced below this (default 5c): long shots can't show a real gap")
    ap.add_argument("--max-price", type=float, default=0.95, help="skip markets priced above this (default 95c)")
    ap.add_argument("--rules", nargs="+", default=["same"],
                    help="rule verdicts to list: same (default), insufficient, not_same, unknown")
    ap.add_argument("--quotes", action="store_true", help="also show both platforms' live prices")
    ap.add_argument("--save", action="store_true",
                    help="save the listed `same`-rule markets to markets.json, which the bot and backtest then use")
    a = ap.parse_args()

    with httpx.Client(timeout=30) as http, ThreadPoolExecutor(6) as pool:
        events = search(http, query=a.query, domain=a.domain, events=a.events)
        found = [c for batch in pool.map(lambda e: event_pairs(http, e["event_id"]), events) for c in batch]
    found = [c for c in found if c.pair.rules in a.rules
             and c.price is not None and a.min_price <= c.price <= a.max_price]
    live = a.sort != "volume" or a.quotes
    if live:
        client = synpath.Client()
        if a.sort == "volume":                     # only the ones that will be shown
            found = sorted(found, key=SORTS["volume"])[:a.limit]
        with ThreadPoolExecutor(8) as pool:
            found = list(pool.map(lambda c: read_books(client, c), found))
    shown = sorted(found, key=SORTS[a.sort])[:a.limit]

    for i, c in enumerate(shown, 1):
        line = f"{i:>3}. {c.pair.rules:<12} {c.pair.name[:56]:<56} {c.pair.kalshi_id} <-> {c.pair.poly_id}  vol ${c.volume:,.0f}"
        if live:
            k, p = c.kalshi, c.poly
            line += (f"  Kalshi {cents(k.bid if k else None)}/{cents(k.ask if k else None)}"
                     f"  Poly {cents(p.bid if p else None)}/{cents(p.ask if p else None)}"
                     f"  gap {cents(c.gap)}  edge {cents(c.edge)}")
        print(line)
    print(f"\nTop {len(shown)} of {len(found)} markets from {len(events)} events, sorted by {a.sort}.")
    same = [c.pair for c in shown if c.pair.rules == "same"]
    if a.save and same:
        MARKETS_FILE.write_text(json.dumps([{"event": p.event_id, "kalshi": p.kalshi_id, "name": p.name}
                                            for p in same], indent=2))
        print(f"Saved {len(same)} markets to {MARKETS_FILE.name}; `python -m trading` and the backtest use them now.")
    elif same:
        print("Run again with --save to use them.")


if __name__ == "__main__":
    main()
