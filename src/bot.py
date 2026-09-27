"""The live bot: every poll, price each configured pair from both venues' live books, open a
position when the strategy says so, and sell it back once the round trip makes the target.

Orders are immediate-or-cancel limits at the price the book quoted, so nothing rests and nothing
fills worse than that price. Both legs of a pair go out together with the same number of
contracts. If one leg fills more than the other, the excess is sold straight back, so what the
bot holds is always matched pairs, never a one-sided bet.

Order sides follow synpath's convention: `buy` takes YES, `sell` takes NO, the price is always the
YES price, and `reduce_only` makes the order sell what is held rather than open the other side.
"""
from __future__ import annotations

import asyncio
import csv
import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import synpath
from synpath import OrderRequest, OrderType, Side, TimeInForce

from .arbitrage import Book, Leg, Opportunity, Pricer
from .matcher import Pair, resolve, tradeable
from .strategy import Strategy

TERMINAL = {"closed", "canceled", "rejected", "expired"}


@dataclass
class Position:
    """A matched pair held on both venues."""
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
    def __init__(self, config: dict[str, Any], *, client: Any = None, trading: dict[str, Any] | None = None):
        self.config = config
        self.strategy = Strategy(config["entry_edge"], config["take_profit"])
        self.client = client or synpath.Client()
        self.trading = trading or {}        # venue -> synpath trading adapter, live mode only
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
            print(f"[PAIR] {pair.name}: {pair.kalshi_id} <-> {pair.poly_id}"
                  f"{' (Polymarket states it the other way round)' if pair.flipped else ''} · rules {pair.rules}")

    def load_state(self) -> None:
        if self.state_file.exists():
            saved = json.loads(self.state_file.read_text())
            self.positions = {k: Position(**v) for k, v in saved.items()}
            for p in self.positions.values():
                print(f"[RESUME] {p.kalshi_id}: {p.contracts:g} pairs held since {p.opened_at}, locked {cents(p.edge)}/pair")

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({k: asdict(v) for k, v in self.positions.items()}, indent=2))

    def record_trade(self, pair: Pair, pos: Position, contracts: float, exit_yes: float, exit_no: float, pnl: float) -> None:
        self.trades_file.parent.mkdir(parents=True, exist_ok=True)
        new = not self.trades_file.exists()
        with open(self.trades_file, "a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["opened_at", "closed_at", "market", "direction", "contracts", "yes_venue", "yes_in",
                            "no_venue", "no_in", "yes_out", "no_out", "pnl_per_pair", "pnl_usd", "dry_run"])
            w.writerow([pos.opened_at, now(), pair.name, pos.direction, contracts, pos.yes_venue, pos.yes_price,
                        pos.no_venue, pos.no_price, exit_yes, exit_no, round(pnl, 4), round(pnl * contracts, 2),
                        self.config["dry_run"]])

    # -- market data ----------------------------------------------------------

    def books(self, pair: Pair) -> tuple[Book, Book]:
        """Both venues' top of book in this proposition's terms."""
        def top(market_id: str) -> Book:
            b = self.client.fetch_order_book(market_id, side="yes", depth=1)
            bid, ask = b.best_bid, b.best_ask
            return Book(bid.price if bid else None, ask.price if ask else None,
                        bid.size if bid else None, ask.size if ask else None)
        kalshi, poly = top(pair.kalshi_id), top(pair.poly_id)
        return kalshi, (poly.flipped() if pair.flipped else poly)

    # -- orders ---------------------------------------------------------------

    def order(self, pair: Pair, venue: str, side: str, price: float, contracts: float, *, closing: bool) -> OrderRequest:
        """One leg as a synpath order. `side` and `price` are in this proposition's terms; on a venue
        that states it the other way round, our YES is its NO."""
        native_yes = (side == "yes") != (venue == "polymarket" and pair.flipped)
        # `price` is what the contract itself costs, whichever way the venue names it; synpath
        # always takes the venue's YES price, so a NO contract at 0.54 is sent as 0.46.
        yes_price = price if native_yes else 1 - price
        if closing:   # sell what is held: YES held -> sell, NO held -> buy, both reduce-only
            order_side = Side.SELL if native_yes else Side.BUY
        else:         # buy: YES -> buy, NO -> sell
            order_side = Side.BUY if native_yes else Side.SELL
        return OrderRequest(market_id=pair.kalshi_id if venue == "kalshi" else pair.poly_id, side=order_side,
                            amount=Decimal(str(contracts)), price=Decimal(str(round(yes_price, 4))),
                            type=OrderType.LIMIT, time_in_force=TimeInForce.IOC, reduce_only=closing,
                            book="cross-venue-arb")

    async def send(self, venue: str, request: OrderRequest) -> float:
        """Contracts filled. A dry run fills everything at the requested price."""
        if self.config["dry_run"]:
            print(f"   [DRY RUN] {venue} {request.side.value}{' reduce-only' if request.reduce_only else ''} "
                  f"{request.amount} @ YES {request.price} on {request.market_id}")
            return float(request.amount)
        client = self.trading[venue]
        try:
            order = await client.create_order(request)
            if order.status.value not in TERMINAL:
                order = await client.fetch_order(order.id)
            return float(order.filled or 0)
        except Exception as exc:        # a refused or failed order fills nothing
            print(f"   [ERROR] {venue}: {type(exc).__name__}: {exc}")
            return 0.0

    async def legs(self, pair: Pair, opp: Opportunity, contracts: float, prices: tuple[float, float], *, closing: bool) -> tuple[float, float]:
        yes_req = self.order(pair, opp.yes.venue, "yes", prices[0], contracts, closing=closing)
        no_req = self.order(pair, opp.no.venue, "no", prices[1], contracts, closing=closing)
        filled = await asyncio.gather(self.send(opp.yes.venue, yes_req), self.send(opp.no.venue, no_req))
        return filled[0], filled[1]

    # -- the strategy ---------------------------------------------------------

    async def enter(self, pair: Pair, opp: Opportunity) -> None:
        size = min(self.config["contracts"], opp.size) if opp.size is not None else self.config["contracts"]
        size = float(int(size))
        if size < 1:
            return
        print(f"[ENTER] {pair.name}: YES {opp.yes.venue} {cents(opp.yes.price)} + NO {opp.no.venue} "
              f"{cents(opp.no.price)} = {cents(opp.cost)} + fees {cents(opp.fees)} · locks {cents(opp.edge)}/pair · {size:g} contracts")
        yes_filled, no_filled = await self.legs(pair, opp, size, (opp.yes.price, opp.no.price), closing=False)
        matched = min(yes_filled, no_filled)
        await self.trim_excess(pair, opp, yes_filled - matched, no_filled - matched)
        if matched <= 0:
            print("   [NO FILL] nothing matched; no position opened")
            return
        self.positions[pair.kalshi_id] = Position(pair.kalshi_id, opp.direction, opp.yes.venue, opp.yes.price,
                                                  opp.no.venue, opp.no.price, opp.fees, matched, opp.edge, now())
        self.save_state()
        print(f"   [OPEN] {matched:g} pairs held; worst case at resolution +${opp.edge * matched:,.2f}")

    async def trim_excess(self, pair: Pair, opp: Opportunity, yes_extra: float, no_extra: float) -> None:
        """Sell back whatever one leg filled beyond the other, at the price it was bought, so only
        matched pairs are held."""
        for side, extra, leg in (("yes", yes_extra, opp.yes), ("no", no_extra, opp.no)):
            if extra > 0:
                print(f"   [TRIM] {side.upper()} filled {extra:g} more than the other leg; selling them back")
                await self.send(leg.venue, self.order(pair, leg.venue, side, leg.price, extra, closing=True))

    async def maybe_exit(self, pair: Pair, pos: Position, kalshi: Book, poly: Book) -> None:
        pricer = self.pricers[pair.kalshi_id]
        opp = pos.as_opportunity()
        round_trip = pricer.unwind(opp, kalshi, poly)
        prices = pricer.exit_prices(opp, kalshi, poly)
        print(f"[HOLD] {pair.name}: {pos.contracts:g} pairs · selling now nets {cents(round_trip)}/pair "
              f"(target {cents(self.strategy.take_profit)}, locked {cents(pos.edge)})")
        if not self.strategy.should_exit(round_trip) or prices is None:
            return
        print(f"[EXIT] {pair.name}: selling YES {pos.yes_venue} at {cents(prices[0])} + NO {pos.no_venue} at {cents(prices[1])}")
        yes_sold, no_sold = await self.legs(pair, opp, pos.contracts, prices, closing=True)
        sold = min(yes_sold, no_sold)
        if sold > 0:
            self.record_trade(pair, pos, sold, prices[0], prices[1], round_trip)
            print(f"   [SOLD] {sold:g} pairs · +${round_trip * sold:,.2f}")
        # Whatever did not sell on both legs stays held, as matched pairs; a leg that sold more than
        # the other is the one-sided remainder, reported so it can be handled.
        left = pos.contracts - sold
        if yes_sold != no_sold:
            print(f"   [WARN] legs sold unevenly (YES {yes_sold:g}, NO {no_sold:g}); check both accounts")
        if left > 0:
            pos.contracts = left
            self.positions[pair.kalshi_id] = pos
        else:
            self.positions.pop(pair.kalshi_id, None)
        self.save_state()

    async def poll(self) -> None:
        for pair in self.pairs:
            try:
                kalshi, poly = await asyncio.to_thread(self.books, pair)
            except Exception as exc:
                print(f"[ERROR] {pair.name}: {type(exc).__name__}: {exc}")
                continue
            pos = self.positions.get(pair.kalshi_id)
            if pos is not None:
                await self.maybe_exit(pair, pos, kalshi, poly)
                continue
            opps = self.pricers[pair.kalshi_id].opportunities(kalshi, poly)
            best = opps[0] if opps else None
            print(f"[{pair.name}] Kalshi {cents(kalshi.bid)}/{cents(kalshi.ask)} · Polymarket {cents(poly.bid)}/{cents(poly.ask)}"
                  f" · best edge {cents(best.edge if best else None)}")
            opp = self.strategy.entry(opps)
            if opp is not None:
                await self.enter(pair, opp)

    async def start(self) -> None:
        mode = "DRY RUN" if self.config["dry_run"] else "LIVE"
        print(f"[BOT STARTED] {mode} · {self.strategy.describe()} · {self.config['contracts']:g} contracts · "
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
