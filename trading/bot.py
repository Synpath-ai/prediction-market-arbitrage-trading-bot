"""The live strategy runner: whenever either platform's order book moves, price that market from
both live books and apply the strategy, paper trading at the quoted prices.

Prices come over the venues' WebSockets (see trading/feed.py), not on a timer: an opportunity is
seen the moment it appears rather than at the next poll.

No orders are sent. An entry signal opens a paper position of equal YES and NO contracts at the
prices the books quote (capped by the size quoted there), and an exit signal closes it at the bids,
recording the round trip. Positions and trades are kept on disk, so a restart picks them up.
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import time
import uuid
from dataclasses import asdict, dataclass
from math import floor, isfinite
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .execution import simulate_fills, simulate_liquidation
import synpath

from src.arbitrage import Book, Leg, Opportunity, Pricer
from src.matcher import Pair, resolve, tradeable
from src.strategy import Strategy

from .feed import LiveFeed
from .events import EventLogger

@dataclass
class Position:
    """A paper position tracking each arbitrage leg independently."""
    kalshi_id: str
    direction: str
    yes_venue: str
    yes_price: float
    yes_fee: float
    no_venue: str
    no_price: float
    no_fee: float
    contracts: float
    edge: float
    opened_at: str
    yes_filled: float | None = None
    no_filled: float | None = None

    # Track consumed top-of-book depth to prevent reusing the same liquidity.
    yes_exit_quote_price: float | None = None
    yes_exit_quote_size: float | None = None
    yes_exit_depth_used: int = 0

    no_exit_quote_price: float | None = None
    no_exit_quote_size: float | None = None
    no_exit_depth_used: int = 0

    def __post_init__(self):
        # Backward compatibility with positions saved before fill tracking.
        if self.yes_filled is None:
            self.yes_filled = self.contracts
        if self.no_filled is None:
            self.no_filled = self.contracts

        # Only fully matched pairs count as arbitrage contracts.
        self.contracts = min(self.yes_filled, self.no_filled)

    @property
    def unmatched_yes(self) -> float:
        return max(0, self.yes_filled - self.no_filled)

    @property
    def unmatched_no(self) -> float:
        return max(0, self.no_filled - self.yes_filled)

    def as_opportunity(self) -> Opportunity:
        return Opportunity(
            self.direction,
            Leg(self.yes_venue, "yes", self.yes_price, self.yes_fee),
            Leg(self.no_venue, "no", self.no_price, self.no_fee),
        )


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def cents(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.1f}¢"


class ArbitrageBot:
    @staticmethod
    def _validate_config(config: dict[str, Any]) -> None:
        """Reject invalid risk limits and feed intervals at startup."""

        def finite_number(
            name: str,
            default: Any,
            *,
            allow_zero: bool = False,
        ) -> float:
            value = config.get(name, default)

            try:
                if isinstance(value, bool):
                    raise ValueError

                number = float(value)

            except (TypeError, ValueError, OverflowError):
                raise ValueError(
                    f"{name} must be a finite numeric value"
                ) from None

            if not isfinite(number):
                raise ValueError(
                    f"{name} must be finite"
                )

            if allow_zero:
                if number < 0:
                    raise ValueError(
                        f"{name} cannot be negative"
                    )
            elif number <= 0:
                raise ValueError(
                    f"{name} must be greater than zero"
                )

            return number

        def positive_integer(name: str, default: Any) -> int:
            value = config.get(name, default)

            try:
                if isinstance(value, bool):
                    raise ValueError

                number = float(value)

            except (TypeError, ValueError, OverflowError):
                raise ValueError(
                    f"{name} must be a positive integer"
                ) from None

            if (
                not isfinite(number)
                or number < 1
                or not number.is_integer()
            ):
                raise ValueError(
                    f"{name} must be a positive integer"
                )

            return int(number)

        positive_integer("contracts", 1)
        positive_integer("max_open_positions", 3)

        finite_number(
            "max_capital_committed",
            100.00,
        )

        finite_number(
            "max_unmatched_exposure",
            10.00,
            allow_zero=True,
        )

        finite_number(
            "max_daily_loss",
            10.00,
        )

        finite_number(
            "kalshi_poll_seconds",
            2.0,
        )

        finite_number(
            "status_interval_seconds",
            60.0,
            allow_zero=True,
        )

    def __init__(self, config: dict[str, Any], *, client: Any = None):
        self._validate_config(config)
        self.config = config
        self.strategy = Strategy(config["entry_edge"], config["take_profit"])
        self.client = client or synpath.Client()
        self.pairs: list[Pair] = []
        self.pricers: dict[str, Pricer] = {}
        self.positions: dict[str, Position] = {}
        self.state_file = Path(config["state_file"])
        self.transaction_file = Path(
            config.get(
                "transaction_file",
                self.state_file.with_suffix(".pending.json"),
            )
        )
        self.trades_file = Path(config["trades_file"])
        self.unmatched_trades_file = Path(
            config.get(
                "unmatched_trades_file",
                self.trades_file.with_name(
                    f"{self.trades_file.stem}_unmatched{self.trades_file.suffix}"
                ),
            )
        )
        self.status_every = float(config.get("status_interval_seconds", 60))
        self._last_status: dict[str, float] = {}
        self._stop = asyncio.Event()
        self.events = EventLogger(
            config.get("event_file", "state/events.jsonl")
        )
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
        self._recover_pending_transaction()

        if not self.state_file.exists():
            return

        saved = json.loads(self.state_file.read_text())
        self.positions = {}

        for key, raw_position in saved.items():
            position_data = dict(raw_position)

            # Migrate positions saved using the old single-fee format.
            legacy_fees = position_data.pop("fees", None)

            if (
                "yes_fee" not in position_data
                or "no_fee" not in position_data
            ):
                if legacy_fees is None:
                    raise ValueError(
                        f"Saved position {key} is missing fee information."
                    )

                fee_per_leg = float(legacy_fees) / 2.0

                position_data.setdefault("yes_fee", fee_per_leg)
                position_data.setdefault("no_fee", fee_per_leg)

            self.positions[key] = Position(**position_data)

        for p in self.positions.values():
            print(
                f"[RESUME] {p.kalshi_id}: "
                f"{p.contracts:g} pairs since {p.opened_at}, "
                f"locked {cents(p.edge)}/pair"
            )

    def save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        temporary_file = self.state_file.with_name(
            f"{self.state_file.name}.tmp"
        )

        try:
            temporary_file.write_text(
                json.dumps(
                    {
                        key: asdict(position)
                        for key, position in self.positions.items()
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

            temporary_file.replace(self.state_file)

        finally:
            try:
                temporary_file.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_json_atomic(
        self,
        path: Path,
        data: dict[str, Any],
    ) -> None:
        """Write JSON through a temporary file and atomically replace the target."""
        path.parent.mkdir(parents=True, exist_ok=True)

        temporary_file = path.with_name(f"{path.name}.tmp")

        try:
            temporary_file.write_text(
                json.dumps(data, indent=2),
                encoding="utf-8",
            )

            temporary_file.replace(path)

        finally:
            try:
                temporary_file.unlink(missing_ok=True)
            except OSError:
                pass

    def _write_csv_transaction_once(
        self,
        path: Path,
        columns: list[str],
        row: dict[str, Any],
        transaction_id: str,
    ) -> None:
        """Atomically append a transaction record without duplicating its ID."""
        if "transaction_id" in columns:
            raise ValueError(
                "transaction_id must be managed by the transaction writer"
            )

        existing_rows = []

        if path.exists() and path.stat().st_size > 0:
            raw_contents = path.read_bytes()

            try:
                text_contents = raw_contents.decode("utf-8-sig")
            except UnicodeDecodeError:
                # Older Windows versions used the cp1252 default encoding.
                text_contents = raw_contents.decode("cp1252")

            reader = csv.DictReader(
                io.StringIO(text_contents, newline="")
            )
            existing_headers = reader.fieldnames

            if not existing_headers:
                raise ValueError(
                    f"Trade history has no CSV header: {path}"
                )

            if not set(columns).issubset(existing_headers):
                raise ValueError(
                    f"Trade history has an incompatible header: {path}"
                )

            existing_rows = list(reader)

            if "transaction_id" in existing_headers:
                if any(
                    old_row.get("transaction_id") == transaction_id
                    for old_row in existing_rows
                ):
                    return
            else:
                existing_headers.append("transaction_id")

            output_headers = existing_headers

        else:
            output_headers = [*columns, "transaction_id"]

        path.parent.mkdir(parents=True, exist_ok=True)

        temporary_file = path.with_name(f"{path.name}.tmp")

        try:
            with temporary_file.open(
                "w",
                newline="",
                encoding="utf-8",
            ) as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=output_headers,
                    extrasaction="ignore",
                )

                writer.writeheader()

                for old_row in existing_rows:
                    writer.writerow(old_row)

                new_row = dict(row)
                new_row["transaction_id"] = transaction_id
                writer.writerow(new_row)

            temporary_file.replace(path)

        finally:
            try:
                temporary_file.unlink(missing_ok=True)
            except OSError:
                pass

    def _finish_pending_transaction(
        self,
        transaction: dict[str, Any],
    ) -> None:
        """Complete a journaled exit transaction idempotently."""
        transaction_id = transaction.get("transaction_id")
        ledger = transaction.get("ledger")
        columns = transaction.get("columns")
        row = transaction.get("row")
        positions = transaction.get("positions")
        event = transaction.get("event")

        if not isinstance(transaction_id, str) or not transaction_id:
            raise ValueError("Pending transaction has no valid ID")

        if ledger == "matched":
            trade_path = self.trades_file
        elif ledger == "unmatched":
            trade_path = self.unmatched_trades_file
        else:
            raise ValueError("Pending transaction has an invalid ledger")

        if not isinstance(columns, list) or not all(
            isinstance(column, str) for column in columns
        ):
            raise ValueError("Pending transaction has invalid CSV columns")

        if not isinstance(row, dict):
            raise ValueError("Pending transaction has an invalid CSV row")

        if not isinstance(positions, dict):
            raise ValueError("Pending transaction has invalid position state")

        if not isinstance(event, dict):
            raise ValueError("Pending transaction has invalid event data")

        if not isinstance(event.get("event_type"), str):
            raise ValueError("Pending transaction has no event type")

        self._write_csv_transaction_once(
            trade_path,
            columns,
            row,
            transaction_id,
        )

        self._write_json_atomic(
            self.state_file,
            positions,
        )

        event_details = dict(event.get("details") or {})
        event_details["transaction_id"] = transaction_id

        self.events.log(
            event["event_type"],
            market=event.get("market"),
            details=event_details,
            raise_on_error=True,
        )

        self.transaction_file.unlink(missing_ok=True)

    def _recover_pending_transaction(self) -> None:
        """Replay an interrupted exit before restoring saved positions."""
        if not self.transaction_file.exists():
            return

        try:
            transaction = json.loads(
                self.transaction_file.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "Cannot recover pending transaction journal"
            ) from exc

        if not isinstance(transaction, dict):
            raise ValueError("Pending transaction journal must contain an object")

        transaction_id = transaction.get("transaction_id")

        self._finish_pending_transaction(transaction)

        print(
            f"[RECOVERY] Completed interrupted transaction "
            f"{transaction_id}"
        )

    def _commit_exit_transaction(
        self,
        *,
        ledger: str,
        columns: list[str],
        row: dict[str, Any],
        event_type: str,
        market: str,
        details: dict[str, Any],
    ) -> str:
        """Journal an exit before committing its trade, state, and event."""
        if self.transaction_file.exists():
            raise RuntimeError(
                "An unfinished transaction must be recovered before "
                "another exit can be committed"
            )

        transaction_id = uuid.uuid4().hex

        transaction = {
            "transaction_id": transaction_id,
            "ledger": ledger,
            "columns": columns,
            "row": row,
            "positions": {
                key: asdict(position)
                for key, position in self.positions.items()
            },
            "event": {
                "event_type": event_type,
                "market": market,
                "details": details,
            },
        }

        try:
            self._write_json_atomic(
                self.transaction_file,
                transaction,
            )

            self._finish_pending_transaction(transaction)

        except Exception:
            self.stop()
            raise

        return transaction_id

    def record_trade(
        self,
        pair: Pair,
        pos: Position,
        exit_yes: float,
        exit_no: float,
        pnl: float,
        closed_contracts: int,
    ) -> None:
        self.trades_file.parent.mkdir(parents=True, exist_ok=True)

        new = (
            not self.trades_file.exists()
            or self.trades_file.stat().st_size == 0
        )

        with self.trades_file.open(
            "a",
            newline="",
            encoding="utf-8",
        ) as f:
            w = csv.writer(f)

            if new:
                w.writerow([
                    "opened_at", "closed_at", "market", "direction",
                    "contracts", "yes_venue", "yes_in", "no_venue",
                    "no_in", "yes_out", "no_out", "pnl_per_pair",
                    "pnl_usd",
                ])

            w.writerow([
                pos.opened_at,
                now(),
                pair.name,
                pos.direction,
                closed_contracts,
                pos.yes_venue,
                pos.yes_price,
                pos.no_venue,
                pos.no_price,
                exit_yes,
                exit_no,
                round(pnl, 4),
                round(pnl * closed_contracts, 2),
            ])

    # -- the strategy ---------------------------------------------------------

    def _capital_committed(self) -> float:
        """Calculate the entry cost, including fees, of all remaining open positions."""
        return sum(
            (pos.yes_price + pos.yes_fee) * pos.yes_filled
            + (pos.no_price + pos.no_fee) * pos.no_filled
            for pos in self.positions.values()
        )

    def _unmatched_exposure(self) -> float:
        """Calculate the entry cost, including fees, of all unmatched contracts."""
        return sum(
            (pos.yes_price + pos.yes_fee) * pos.unmatched_yes
            + (pos.no_price + pos.no_fee) * pos.unmatched_no
            for pos in self.positions.values()
        )

    def _daily_realized_pnl(self) -> float | None:
        """Sum today's matched and unmatched realized P&L in UTC.

        Return None if trade history cannot be read safely.
        """
        today = datetime.now(timezone.utc).date()
        total = 0.0

        for path in (self.trades_file, self.unmatched_trades_file):
            if not path.exists():
                continue

            try:
                with open(path, newline="", encoding="utf-8") as file:
                    reader = csv.DictReader(file)

                    required_columns = {"closed_at", "pnl_usd"}
                    if (
                        not reader.fieldnames
                        or not required_columns.issubset(reader.fieldnames)
                    ):
                        return None

                    for row in reader:
                        closed_at = row.get("closed_at")

                        try:
                            closed_date = datetime.strptime(
                                closed_at, "%Y-%m-%d %H:%M:%S UTC"
                            ).date()
                        except (TypeError, ValueError):
                            return None

                        if closed_date != today:
                            continue

                        raw_pnl = row.get("pnl_usd")

                        if raw_pnl is None or not raw_pnl.strip():
                            return None

                        try:
                            trade_pnl = float(raw_pnl)
                        except (TypeError, ValueError):
                            return None

                        if not isfinite(trade_pnl):
                            return None

                        total += trade_pnl

            except (OSError, csv.Error, UnicodeError):
                return None

        return total

    def _daily_loss_block_reason(self) -> str | None:
        """Return a reason to block new entries when the daily loss limit is reached."""
        max_daily_loss = float(
            self.config.get("max_daily_loss", 10.00)
        )

        if not isfinite(max_daily_loss) or max_daily_loss <= 0:
            return "daily loss limit is invalid"

        daily_pnl = self._daily_realized_pnl()

        if daily_pnl is None:
            return "today's realized P&L cannot be verified"

        if daily_pnl <= -max_daily_loss:
            return (
                f"daily loss limit reached "
                f"(${abs(daily_pnl):.2f} loss / "
                f"${max_daily_loss:.2f} limit)"
            )

        return None

    def enter(self, pair: Pair, opp: Opportunity) -> None:
        if self.config.get("emergency_stop", False):
            print(
                f"[ENTRY BLOCKED] {pair.name}: "
                "emergency stop is enabled."
            )
            return

        daily_loss_reason = self._daily_loss_block_reason()

        if daily_loss_reason is not None:
            print(
                f"[ENTRY BLOCKED] {pair.name}: "
                f"{daily_loss_reason}."
            )
            return

        requested = int(self.config["contracts"])

        # Convert available book depth to whole contracts.
        yes_available = (
            floor(opp.yes.size) if opp.yes.size is not None else requested
        )
        no_available = (
            floor(opp.no.size) if opp.no.size is not None else requested
        )

        # Simulate each leg independently.
        fills = simulate_fills(
            requested,
            yes_available,
            no_available,
        )

        if fills.yes_filled == 0 and fills.no_filled == 0:
            return

        max_capital = float(
            self.config.get("max_capital_committed", 100.00)
        )
        current_capital = self._capital_committed()

        new_capital = (
            (opp.yes.price + opp.yes.fee) * fills.yes_filled
            + (opp.no.price + opp.no.fee) * fills.no_filled
        )

        if current_capital + new_capital > max_capital:
            print(
                f"[ENTRY BLOCKED] {pair.name}: capital limit would be exceeded "
                f"(${current_capital + new_capital:.2f} / ${max_capital:.2f})."
            )
            return

        max_unmatched = float(
            self.config.get("max_unmatched_exposure", 10.00)
        )

        current_unmatched = self._unmatched_exposure()

        new_unmatched = (
            (opp.yes.price + opp.yes.fee) * fills.unmatched_yes
            + (opp.no.price + opp.no.fee) * fills.unmatched_no
        )

        if current_unmatched + new_unmatched > max_unmatched:
            print(
                f"[ENTRY BLOCKED] {pair.name}: unmatched exposure limit "
                f"would be exceeded "
                f"(${current_unmatched + new_unmatched:.2f} / "
                f"${max_unmatched:.2f})."
            )
            return

        self.positions[pair.kalshi_id] = Position(
            kalshi_id=pair.kalshi_id,
            direction=opp.direction,
            yes_venue=opp.yes.venue,
            yes_price=opp.yes.price,
            yes_fee=opp.yes.fee,
            no_venue=opp.no.venue,
            no_price=opp.no.price,
            no_fee=opp.no.fee,
            contracts=fills.matched,
            yes_filled=fills.yes_filled,
            no_filled=fills.no_filled,
            edge=opp.edge,
            opened_at=now(),
        )

        self.save_state()
        self.events.log(
            "paper_entry",
            market=pair.name,
            details={
                "kalshi_id": pair.kalshi_id,
                "direction": opp.direction,
                "yes_venue": opp.yes.venue,
                "yes_price": opp.yes.price,
                "no_venue": opp.no.venue,
                "no_price": opp.no.price,
                "requested": requested,
                "yes_filled": fills.yes_filled,
                "no_filled": fills.no_filled,
                "matched": fills.matched,
                "edge": opp.edge,
            },
        )

        print(
            f"[ENTRY] {pair.name}: "
            f"YES filled {fills.yes_filled} "
            f"· NO filled {fills.no_filled} "
            f"· matched {fills.matched} "
            f"· unmatched YES {fills.unmatched_yes} "
            f"· unmatched NO {fills.unmatched_no}"
        )

    def _available_exit_depth(
        self,
        pos: Position,
        side: str,
        venue: str,
        books: dict[str, Book],
    ) -> tuple[float | None, int]:
        """Return the exit price and unused displayed depth."""
        book = books[venue]

        if side == "yes":
            price = book.bid
            size = book.bid_size
        else:
            price = book.no_bid
            size = book.ask_size

        if price is None or size is None:
            return None, 0

        price_field = f"{side}_exit_quote_price"
        size_field = f"{side}_exit_quote_size"
        used_field = f"{side}_exit_depth_used"

        # A changed quote is treated as new displayed liquidity.
        if (
            getattr(pos, price_field) != price
            or getattr(pos, size_field) != size
        ):
            setattr(pos, price_field, price)
            setattr(pos, size_field, size)
            setattr(pos, used_field, 0)

        used = getattr(pos, used_field)
        remaining = max(0, floor(size) - used)

        return price, remaining

    def _consume_exit_depth(
        self,
        pos: Position,
        side: str,
        quantity: int,
    ) -> None:
        """Record displayed liquidity consumed by a simulated exit."""
        field = f"{side}_exit_depth_used"
        setattr(pos, field, getattr(pos, field) + quantity)



    def liquidate_unmatched(
        self, pair: Pair, pos: Position, kalshi: Book, poly: Book
    ) -> None:
        """Paper-liquidate unmatched exposure using shared exit depth."""
        pricer = self.pricers[pair.kalshi_id]
        books = {"kalshi": kalshi, "polymarket": poly}

        def liquidate_leg(
            side: str,
            venue: str,
            quantity: float,
            entry_price: float,
            entry_fee: float,
        ) -> None:
            if quantity <= 0:
                return

            exit_price, remaining_depth = self._available_exit_depth(
                pos, side, venue, books
            )

            if exit_price is None:
                return

            exposure_quantity = floor(quantity)
            liquidation_quantity = min(exposure_quantity, remaining_depth)

            if liquidation_quantity <= 0:
                return

            exit_leg = pricer.leg(
                venue,
                side,
                exit_price,
                contracts=liquidation_quantity,
            )

            if exit_leg is None:
                return

            result = simulate_liquidation(
                quantity=exposure_quantity,
                available_exit_depth=remaining_depth,
                entry_price=entry_price,
                entry_fee=entry_fee,
                exit_price=exit_price,
                exit_fee=exit_leg.fee,
            )

            if result.liquidated <= 0:
                return

            self._consume_exit_depth(
                pos, side, result.liquidated
            )

            if side == "yes":
                pos.yes_filled -= result.liquidated
            else:
                pos.no_filled -= result.liquidated

            pos.contracts = min(pos.yes_filled, pos.no_filled)

            # Remove fully liquidated positions before recording the
            # transaction so recovery cannot restore an empty position.
            if pos.yes_filled <= 0 and pos.no_filled <= 0:
                self.positions.pop(pair.kalshi_id, None)

            closed_at = now()
            self._commit_exit_transaction(
                ledger="unmatched",
                columns=[
                    "opened_at",
                    "closed_at",
                    "market",
                    "leg",
                    "venue",
                    "contracts",
                    "entry_price",
                    "exit_price",
                    "entry_fee_per_contract",
                    "exit_fee_per_contract",
                    "pnl_usd",
                ],
                row={
                    "opened_at": pos.opened_at,
                    "closed_at": closed_at,
                    "market": pair.name,
                    "leg": side,
                    "venue": venue,
                    "contracts": result.liquidated,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "entry_fee_per_contract": entry_fee,
                    "exit_fee_per_contract": exit_leg.fee,
                    "pnl_usd": (
                        ""
                        if result.pnl is None
                        else round(result.pnl, 4)
                    ),
                },
                event_type="unmatched_exit",
                market=pair.name,
                details={
                    "leg": side,
                    "venue": venue,
                    "contracts_liquidated": result.liquidated,
                    "exit_price": exit_price,
                    "remaining_exposure": result.remaining,
                    "pnl": result.pnl,
                },
            )

            print(
                f"[UNMATCHED EXIT] {pair.name}: {side.upper()} "
                f"{result.liquidated} sold at {exit_price:.4f} "
                f"· remaining {result.remaining} · P&L "
                f"{'unknown' if result.pnl is None else f'${result.pnl:.2f}'}"
            )

        if pos.unmatched_yes > 0:
            liquidate_leg(
                "yes",
                pos.yes_venue,
                pos.unmatched_yes,
                pos.yes_price,
                pos.yes_fee,
            )

        if pos.unmatched_no > 0:
            liquidate_leg(
                "no",
                pos.no_venue,
                pos.unmatched_no,
                pos.no_price,
                pos.no_fee,
            )

        if pos.yes_filled <= 0 and pos.no_filled <= 0:
            self.positions.pop(pair.kalshi_id, None)

        self.save_state()



    def maybe_exit(self, pair: Pair, pos: Position, kalshi: Book, poly: Book) -> None:
        pricer = self.pricers[pair.kalshi_id]
        books = {"kalshi": kalshi, "polymarket": poly}

        # Always manage unmatched exposure before matched-pair exits.
        self.liquidate_unmatched(pair, pos, kalshi, poly)

        # Unmatched liquidation may have changed the position.
        if pos.contracts <= 0:
            remaining = self.positions.get(pair.kalshi_id)

            if remaining is not None and self._status_due(pair):
                print(
                    f"[UNMATCHED] {pair.name}: "
                    f"YES exposure {remaining.unmatched_yes:g} "
                    f"· NO exposure {remaining.unmatched_no:g}"
                )
            return

        # Get remaining displayed depth for both exit legs.
        yes_price, yes_depth = self._available_exit_depth(
            pos, "yes", pos.yes_venue, books
        )
        no_price, no_depth = self._available_exit_depth(
            pos, "no", pos.no_venue, books
        )

        if yes_price is None or no_price is None:
            return

        # Both legs must have enough depth to close a matched pair.
        closed = min(
            floor(pos.contracts),
            yes_depth,
            no_depth,
        )

        if closed <= 0:
            if self._status_due(pair):
                print(
                    f"[HOLD] {pair.name}: insufficient displayed exit depth"
                )
            return

        # Calculate fees using the actual quantity being closed.
        yes_exit_leg = pricer.leg(
            pos.yes_venue,
            "yes",
            yes_price,
            contracts=closed,
        )
        no_exit_leg = pricer.leg(
            pos.no_venue,
            "no",
            no_price,
            contracts=closed,
        )

        if (
            yes_exit_leg is None
            or no_exit_leg is None
            or yes_exit_leg.fee is None
            or no_exit_leg.fee is None
        ):
            return

        # Calculate net round-trip P&L per matched pair.
        round_trip = (
            yes_price
            + no_price
            - pos.yes_price
            - pos.no_price
            - pos.yes_fee
            - pos.no_fee
            - yes_exit_leg.fee
            - no_exit_leg.fee
        )

        if not self.strategy.should_exit(round_trip):
            if self._status_due(pair):
                print(
                    f"[HOLD] {pair.name}: {pos.contracts:g} matched pairs "
                    f"· selling now nets {cents(round_trip)}/pair "
                    f"(target {cents(self.strategy.take_profit)})"
                )
            return

        # Update the position before journaling the completed exit.
        self._consume_exit_depth(pos, "yes", closed)
        self._consume_exit_depth(pos, "no", closed)

        pos.yes_filled -= closed
        pos.no_filled -= closed
        pos.contracts = min(pos.yes_filled, pos.no_filled)

        remaining_contracts = pos.contracts

        if pos.yes_filled <= 0 and pos.no_filled <= 0:
            self.positions.pop(pair.kalshi_id, None)
            print(
                f"[EXIT] {pair.name}: {closed} matched pairs closed "
                f"· {cents(round_trip)}/pair "
                f"· ${round_trip * closed:,.2f}"
            )
        else:
            print(
                f"[PARTIAL EXIT] {pair.name}: {closed} matched pairs closed "
                f"· remaining YES {pos.yes_filled:g} "
                f"· remaining NO {pos.no_filled:g}"
            )

        closed_at = now()

        self._commit_exit_transaction(
            ledger="matched",
            columns=[
                "opened_at",
                "closed_at",
                "market",
                "direction",
                "contracts",
                "yes_venue",
                "yes_in",
                "no_venue",
                "no_in",
                "yes_out",
                "no_out",
                "pnl_per_pair",
                "pnl_usd",
            ],
            row={
                "opened_at": pos.opened_at,
                "closed_at": closed_at,
                "market": pair.name,
                "direction": pos.direction,
                "contracts": closed,
                "yes_venue": pos.yes_venue,
                "yes_in": pos.yes_price,
                "no_venue": pos.no_venue,
                "no_in": pos.no_price,
                "yes_out": yes_price,
                "no_out": no_price,
                "pnl_per_pair": round(round_trip, 4),
                "pnl_usd": round(round_trip * closed, 2),
            },
            event_type="paper_exit",
            market=pair.name,
            details={
                "direction": pos.direction,
                "contracts_closed": closed,
                "yes_exit_price": yes_price,
                "no_exit_price": no_price,
                "pnl_per_pair": round_trip,
                "total_pnl": round(round_trip * closed, 4),
                "remaining_contracts": remaining_contracts,
            },
        )



    def evaluate(self, pair: Pair, kalshi: Book, poly: Book) -> None:
        """Apply the strategy to one market's current books: exit or hold an open position,
        else enter if the edge is there."""
        pos = self.positions.get(pair.kalshi_id)
        if pos is not None:
            self.maybe_exit(pair, pos, kalshi, poly)
            return

        if self.config.get("emergency_stop", False):
            if self._status_due(pair):
                print(
                    f"[{pair.name}] New entry blocked: "
                    "emergency stop is enabled."
                )
            return

        daily_loss_reason = self._daily_loss_block_reason()

        if daily_loss_reason is not None:
            if self._status_due(pair):
                print(
                    f"[{pair.name}] New entry blocked: "
                    f"{daily_loss_reason}."
                )
            return

        max_open_positions = int(self.config.get("max_open_positions", 3))
        if len(self.positions) >= max_open_positions:
            if self._status_due(pair):
                print(
                    f"[{pair.name}] New entry blocked: maximum open positions "
                    f"reached ({max_open_positions})."
                )
            return
        max_capital = float(self.config.get("max_capital_committed", 100.00))
        current_capital = self._capital_committed()

        if current_capital >= max_capital:
            if self._status_due(pair):
                print(
                    f"[{pair.name}] New entry blocked: capital limit reached "
                    f"(${current_capital:.2f} / ${max_capital:.2f})."
                )
            return
        opps = self.pricers[pair.kalshi_id].opportunities(
            kalshi,
            poly,
            require_depth=True,
        )
        best = opps[0] if opps else None
        opp = self.strategy.entry(opps)
        if opp is not None:
            self.enter(pair, opp)
        elif self._status_due(pair):
            print(f"[{pair.name}] Kalshi {cents(kalshi.bid)}/{cents(kalshi.ask)} · Polymarket {cents(poly.bid)}/{cents(poly.ask)}"
                  f" · best edge {cents(best.edge if best else None)}")

    def _status_due(self, pair: Pair) -> bool:
        """At most one status line per market per `status_interval_seconds`: books move many
        times a second, entries and exits are always printed."""
        now_s = time.monotonic()
        if now_s - self._last_status.get(pair.kalshi_id, float("-inf")) < self.status_every:
            return False
        self._last_status[pair.kalshi_id] = now_s
        return True

    async def start(self, feed: LiveFeed | None = None) -> None:
        self.load_pairs()
        self.load_state()
        if not self.pairs:
            print("[STOPPED] no tradeable markets")
            return
        if feed is None:
            kalshi_credentials = None
            try:
                kalshi_credentials = synpath.load_credentials().get("kalshi")
            except Exception as exc:  # a half-configured key: say so, and poll Kalshi instead
                print(f"[FEED] Kalshi credentials not loaded ({exc}); reading Kalshi over REST")
            feed = LiveFeed(
                self.pairs,
                client=self.client,
                kalshi_credentials=kalshi_credentials,
                kalshi_poll_seconds=self.config.get("kalshi_poll_seconds", 2),
            )

        print(
            f"[STARTED] paper trading · {self.strategy.describe()} · "
            f"{self.config['contracts']:g} contracts · "
            f"Polymarket over WebSocket · Kalshi over {feed.kalshi_mode}\n"
        )

        self.events.log(
            "session_start",
            details={
                "markets_loaded": len(self.pairs),
                "contracts": self.config["contracts"],
                "kalshi_mode": feed.kalshi_mode,
            },
        )

        await feed.start()

        changes = asyncio.create_task(self._run(feed))
        stop_waiter = asyncio.create_task(self._stop.wait())

        try:
            done, _ = await asyncio.wait(
                {changes, stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Propagate unexpected trading-loop exceptions.
            if changes in done:
                await changes

        finally:
            # Cancel and await the stop waiter.
            stop_waiter.cancel()

            await asyncio.gather(
                stop_waiter,
                return_exceptions=True,
            )

            # Stop the trading loop if it is still running.
            if not changes.done():
                changes.cancel()

            # Retrieve any remaining task result.
            await asyncio.gather(
                changes,
                return_exceptions=True,
            )

            # Always log session_stop, even if feed shutdown fails.
            try:
                await feed.close()
            finally:
                self.events.log(
                    "session_stop",
                    details={
                        "markets_loaded": len(self.pairs),
                    },
                )

    async def _run(self, feed: LiveFeed) -> None:
        async for pair in feed.changes():
            books = feed.books(pair)
            if books is None:
                continue
            try:
                self.evaluate(pair, *books)
            except Exception as exc:
                print(f"[ERROR] {pair.name}: {type(exc).__name__}: {exc}")
                self.events.log(
                    "runtime_error",
                    market=pair.name,
                    details={
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    },
                )
    def stop(self) -> None:
        self._stop.set()