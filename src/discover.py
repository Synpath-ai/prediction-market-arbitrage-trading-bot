"""Find markets to trade: Kalshi listings that Polymarket lists as the same proposition, with how
the two venues' rules compare and where both venues price them now.

    python -m src.discover                         # the most traded matched events
    python -m src.discover --query "senate"        # events matching a search
    python -m src.discover --domain election --events 50 --rules same

Prints one line per pair and, at the end, entries ready to paste into config.py's `markets`.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor

import httpx
import synpath

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


def event_pairs(http: httpx.Client, event_id: str) -> list[Pair]:
    detail = http.get(f"{CATALOG}/events/{event_id}").raise_for_status().json()
    out = []
    for outcome in detail["outcomes"]:
        kalshi = next((c for c in outcome["contracts"] if c["venue"] == "kalshi" and c.get("primary")), None)
        if kalshi is None:
            continue
        try:
            out.append(resolve(event_id, f"kalshi:{kalshi['native_market_id']}", http=http))
        except LookupError:
            continue                     # no Polymarket listing asks the same question
    return out


def quote(client: synpath.Client, market_id: str) -> str:
    try:
        b = client.fetch_order_book(market_id, side="yes", depth=1)
    except Exception:
        return "—"
    bid = f"{b.best_bid.price * 100:.1f}" if b.best_bid else "—"
    ask = f"{b.best_ask.price * 100:.1f}" if b.best_ask else "—"
    return f"{bid}/{ask}¢"


def main() -> None:
    ap = argparse.ArgumentParser(description="List Kalshi/Polymarket pairs of the same market")
    ap.add_argument("--query", help="search text")
    ap.add_argument("--domain", help="election, sports, econ, fed, crypto, weather, ...")
    ap.add_argument("--events", type=int, default=30, help="events to look through")
    ap.add_argument("--rules", nargs="+", default=["same", "insufficient", "not_same", "unknown"],
                    help="rule verdicts to list (only `same` is traded by default)")
    ap.add_argument("--quotes", action="store_true", help="also read both venues' books now (slower)")
    a = ap.parse_args()

    with httpx.Client(timeout=30) as http, ThreadPoolExecutor(6) as pool:
        events = search(http, query=a.query, domain=a.domain, events=a.events)
        pairs = [p for batch in pool.map(lambda e: event_pairs(http, e["event_id"]), events) for p in batch]
    pairs = [p for p in pairs if p.rules in a.rules]
    client = synpath.Client() if a.quotes else None
    for p in pairs:
        line = f"{p.rules:<12} {p.name[:60]:<60} {p.kalshi_id} <-> {p.poly_id}"
        if client:
            line += f"   Kalshi {quote(client, p.kalshi_id)}  Polymarket {quote(client, p.poly_id)}"
        print(line)
    same = [p for p in pairs if p.rules == "same"]
    print(f"\n{len(pairs)} pairs from {len(events)} events; {len(same)} with rules compared as `same`.")
    if same:
        print("\nconfig.py entries:")
        for p in same:
            print(f'        {{"event": "{p.event_id}", "kalshi": "{p.kalshi_id}"}},   # {p.name}')


if __name__ == "__main__":
    main()
