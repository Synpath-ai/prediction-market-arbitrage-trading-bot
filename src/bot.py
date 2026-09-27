"""The live strategy runner: every poll, price each configured market from both platforms' live
order books and apply the strategy, paper trading at the quoted prices.

No orders are sent. An entry signal opens a paper position of equal YES and NO contracts at the
prices the books quote (capped by the size quoted there), and an exit signal closes it at the bids,
recording the round trip. Positions and trades are kept on disk, so a restart picks them up.
"""
from __future__ import annotations

import asyncio
import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import synpath

from .arbitrage import Book, Leg, Opportunity, Pricer
from .matcher import Pair, resolve, tradeable
from .strategy import Strategy


@dataclass
class Position:
    """A paper position: equal YES and NO contracts on the two platforms."""
    kalshi_id: str
    direction: str
    yes_venue: str
    yes_price: float
    no_venue: str
    no_price: float
    fees: float           # entry fees per pair
    contracts: float
    edge: float           # locked at entry, per pair
    opened_at: str

    def as_opportunity(self) -> Opportunity:
        return Opportunity(self.direction, Leg(self.yes_venue, "yes", self.yes_price, self.fees / 2),
                           Leg(self.no_venue, "no", self.no_price, self.fees / 2))


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def cents(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}¢"


class ArbitrageBot:
    def __init__(self, config: dict[str, Any], *, client: Any = None):
        self.config = config
        self.strategy = Strategy(config["entry_edge"], config["take_profit"])
        self.client = client or synpath.Client()
        self.pairs: list[Pair] = []
        self.pricers: dict[str, Pricer] = {}
        self.positions: dict[str, Position] = {}
        self.state_file = Path(config["state_file"])
        self.trades_file = Path(config["trades_file"])
        self._stop = asyncio.Event()

    # -- setup ----------------------------------------------------------------

    def load_pairs(self) -> None:
        for market in self.config["markets"]:
            pair = resolve(market["event"], market["kalshi"])
            if not tradeable(pair, require_same=self.config["require_same_rules"]):
                print(f"[SKIP] {pair.name}: rules are '{pair.rules}' ({pair.rules_reason or 'no comparison'})")
                continue
            self.pairs.append(pair)
            self.pricers[pair.kalshi_id] = Pricer(self.client.fetch_fee_schedule(pair.kalshi_id),
                                                  self.client.fetch_fee_schedule(pair.poly_id),
                                                  contracts=self.config["contracts"])
            print(f"[MARKET] {pair.name}: {pair.kalshi_id} <-> {pair.poly_id}"
                  f"{' (Polymarket states it the other way round)' if pair.flipped else ''} · rules {pair.rules}")

    def load_state(self) -> None:
        if self.state_file.exists():
            saved = json.loads(self.state_file.read_text())
            self.positions = {k: Position(**v) for k, v in saved.items()}
            for p in self.positions.values():
                print(f"[RESUME] {p.kalshi_id}: {p.contracts:g} pairs since {p.opened_at}, locked {cents(p.edge)}/pair")

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({k: asdict(v) for k, v in self.positions.items()}, indent=2))

    def record_trade(self, pair: Pair, pos: Position, exit_yes: float, exit_no: float, pnl: float) -> None:
        self.trades_file.parent.mkdir(parents=True, exist_ok=True)
        new = not self.trades_file.exists()
        with open(self.trades_file, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["opened_at", "closed_at", "market", "direction", "contracts", "yes_venue", "yes_in",
                            "no_venue", "no_in", "yes_out", "no_out", "pnl_per_pair", "pnl_usd"])
            w.writerow([pos.opened_at, now(), pair.name, pos.direction, pos.contracts, pos.yes_venue, pos.yes_price,
                        pos.no_venue, pos.no_price, exit_yes, exit_no, round(pnl, 4), round(pnl * pos.contracts, 2)])

    # -- market data ----------------------------------------------------------

    def books(self, pair: Pair) -> tuple[Book, Book]:
        """Both platforms' top of book in this market's terms."""
        def top(market_id: str) -> Book:
            b = self.client.fetch_order_book(market_id, side="yes", depth=1)
            bid, ask = b.best_bid, b.best_ask
            return Book(bid.price if bid else None, ask.price if ask else None,
                        bid.size if bid else None, ask.size if ask else None)
        kalshi, poly = top(pair.kalshi_id), top(pair.poly_id)
        return kalshi, (poly.flipped() if pair.flipped else poly)

    # -- the strategy ---------------------------------------------------------

    def enter(self, pair: Pair, opp: Opportunity) -> None:
        size = min(self.config["contracts"], opp.size) if opp.size is not None else self.config["contracts"]
        size = float(int(size))
        if size < 1:
            return
        self.positions[pair.kalshi_id] = Position(pair.kalshi_id, opp.direction, opp.yes.venue, opp.yes.price,
                                                  opp.no.venue, opp.no.price, opp.fees, size, opp.edge, now())
        self.save_state()
        print(f"[ENTRY] {pair.name}: YES {opp.yes.venue} {cents(opp.yes.price)} + NO {opp.no.venue} "
              f"{cents(opp.no.price)} = {cents(opp.cost)} + fees {cents(opp.fees)} · locks {cents(opp.edge)}/pair "
              f"· {size:g} contracts (${opp.edge * size:,.2f} at resolution)")

    def maybe_exit(self, pair: Pair, pos: Position, kalshi: Book, poly: Book) -> None:
        pricer = self.pricers[pair.kalshi_id]
        opp = pos.as_opportunity()
        round_trip = pricer.unwind(opp, kalshi, poly)
        prices = pricer.exit_prices(opp, kalshi, poly)
        if not self.strategy.should_exit(round_trip) or prices is None:
            print(f"[HOLD] {pair.name}: {pos.contracts:g} pairs · selling now nets {cents(round_trip)}/pair "
                  f"(target {cents(self.strategy.take_profit)}, locked {cents(pos.edge)})")
            return
        self.record_trade(pair, pos, prices[0], prices[1], round_trip)
        self.positions.pop(pair.kalshi_id, None)
        self.save_state()
        print(f"[EXIT] {pair.name}: sell YES {pos.yes_venue} {cents(prices[0])} + NO {pos.no_venue} {cents(prices[1])}"
              f" · {cents(round_trip)}/pair · ${round_trip * pos.contracts:,.2f}")

    async def poll(self) -> None:
        for pair in self.pairs:
            try:
                kalshi, poly = await asyncio.to_thread(self.books, pair)
            except Exception as exc:
                print(f"[ERROR] {pair.name}: {type(exc).__name__}: {exc}")
                continue
            pos = self.positions.get(pair.kalshi_id)
            if pos is not None:
                self.maybe_exit(pair, pos, kalshi, poly)
                continue
            opps = self.pricers[pair.kalshi_id].opportunities(kalshi, poly)
            best = opps[0] if opps else None
            print(f"[{pair.name}] Kalshi {cents(kalshi.bid)}/{cents(kalshi.ask)} · Polymarket {cents(poly.bid)}/{cents(poly.ask)}"
                  f" · best edge {cents(best.edge if best else None)}")
            opp = self.strategy.entry(opps)
            if opp is not None:
                self.enter(pair, opp)

    async def start(self) -> None:
        print(f"[STARTED] paper trading · {self.strategy.describe()} · {self.config['contracts']:g} contracts · "
              f"every {self.config['poll_interval_seconds']}s\n")
        self.load_pairs()
        self.load_state()
        while not self._stop.is_set():
            print(f"\n[{now()}]")
            await self.poll()
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config["poll_interval_seconds"])
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
