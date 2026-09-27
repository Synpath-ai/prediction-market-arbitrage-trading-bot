"""Backtest the strategy on each configured pair's hourly history.

    python -m backtest.backtest                     # 60 days, config.py's markets and settings
    python -m backtest.backtest --days 90 --source trades

Hour by hour, one position per pair, with the same Pricer and Strategy the live bot uses: enter
when both sides lock the entry edge after fees, sell when the round trip nets the take-profit,
otherwise hold. A position still open when the data ends is reported as held, at the edge it
locked (what it pays at resolution) and at what selling it at the last hour would have made.

Writes out/backtest.json (series and trades per pair, read by backtest/charts.py) and prints a
summary in dollars at the configured size.
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import synpath

from config import CONFIG
from src.arbitrage import Book, Pricer
from src.matcher import resolve, tradeable
from src.strategy import Strategy

from .data import DAY_MS, HOUR_MS, history

OUT = Path("out")


def iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def simulate(rows: list[tuple[int, Book, Book]], pricer: Pricer, strategy: Strategy) -> list[dict]:
    """Every trade the strategy would have made on these hours, in order."""
    trades, pos = [], None
    for t, k, p in rows:
        if pos is not None:
            t0, opp = pos
            round_trip = pricer.unwind(opp, k, p)
            if t > t0 and strategy.should_exit(round_trip):
                trades.append(_trade(t0, opp, t, round_trip))
                pos = None
            continue
        opp = strategy.entry(pricer.opportunities(k, p))
        if opp is not None:
            pos = (t, opp)
    if pos is not None:
        t0, opp = pos
        t_end, k, p = rows[-1]
        trades.append({**_trade(t0, opp, None, opp.edge), "mark": pricer.unwind(opp, k, p), "held_to": t_end})
    return trades


def _trade(t0: int, opp, t1: int | None, pnl: float) -> dict:
    return {
        "entry_ms": t0, "exit_ms": t1, "direction": opp.direction,
        "yes_venue": opp.yes.venue, "yes_price": opp.yes.price, "no_venue": opp.no.venue, "no_price": opp.no.price,
        "cost": round(opp.cost, 4), "fees": round(opp.fees, 5), "edge": opp.edge, "pnl": pnl,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--source", choices=("quotes", "trades"), default="quotes", help="Polymarket history")
    ap.add_argument("--contracts", type=float, default=CONFIG["contracts"])
    ap.add_argument("--entry-edge", type=float, default=CONFIG["entry_edge"])
    ap.add_argument("--take-profit", type=float, default=CONFIG["take_profit"])
    a = ap.parse_args()

    strategy = Strategy(a.entry_edge, a.take_profit)
    until = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    since = until - a.days * DAY_MS
    client = synpath.Client()
    out = {"generated_at": iso(until), "since": iso(since), "source": a.source, "contracts": a.contracts,
           "strategy": strategy.describe(), "pairs": []}
    print(f"{strategy.describe()}  ·  {a.contracts:g} contracts  ·  {a.days}d hourly, Polymarket {a.source}\n")
    total = 0.0
    for market in CONFIG["markets"]:
        pair = resolve(market["event"], market["kalshi"])
        if not tradeable(pair, require_same=CONFIG["require_same_rules"]):
            print(f"skip {pair.name}: rules '{pair.rules}'")
            continue
        pricer = Pricer(client.fetch_fee_schedule(pair.kalshi_id), client.fetch_fee_schedule(pair.poly_id), contracts=a.contracts)
        rows = history(pair, since, until, source=a.source)
        trades = simulate(rows, pricer, strategy)
        closed = [t for t in trades if t["exit_ms"] is not None]
        held = [t for t in trades if t["exit_ms"] is None]
        pnl = sum(t["pnl"] for t in closed) * a.contracts
        total += pnl
        print(f"{pair.name:44} {len(closed):>3} closed, {sum(t['pnl'] > 0 for t in closed):>3} won  "
              f"${pnl:>9,.2f}" + (f"   {len(held)} held (locked ${sum(t['edge'] for t in held) * a.contracts:,.2f})" if held else ""))
        out["pairs"].append({
            "name": pair.name, "event": pair.event, "outcome": pair.outcome, "kalshi_id": pair.kalshi_id,
            "poly_id": pair.poly_id, "rules": pair.rules,
            "series": [[t, k.bid, k.ask, p.mid] for t, k, p in rows], "trades": trades,
        })
    print(f"\n{'total':44} ${total:>27,.2f}")
    OUT.mkdir(exist_ok=True)
    (OUT / "backtest.json").write_text(json.dumps(out, separators=(",", ":")))
    print(f"wrote {OUT / 'backtest.json'}")


if __name__ == "__main__":
    main()
