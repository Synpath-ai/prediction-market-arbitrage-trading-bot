

"""Summarize genuine paper-trading sessions from the structured event log."""

import json
from collections import Counter
from pathlib import Path


EVENT_FILE = Path("state/events.jsonl")


def load_events(path: Path = EVENT_FILE) -> list[dict]:
    """Load structured events from a JSON Lines file."""
    if not path.exists():
        return []

    events = []

    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue

            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                print(f"[WARNING] Skipping invalid JSON on line {line_number}")

    return events


def filter_session_events(events: list[dict]) -> list[dict]:
    """Keep events recorded during genuine paper-trading sessions."""
    session_events = []
    session_active = False

    for event in events:
        event_type = event.get("event_type")

        if event_type == "session_start":
            session_active = True
            session_events.append(event)

        elif event_type == "session_stop":
            if session_active:
                session_events.append(event)
            session_active = False

        elif session_active:
            session_events.append(event)

    return session_events



def summarize(events: list[dict]) -> dict:
    """Calculate summary statistics without double-counting replayed transactions."""
    unique_events = []
    seen_transactions = set()

    for event in events:
        event_type = event.get("event_type")
        details = event.get("details") or {}
        transaction_id = details.get("transaction_id")

        if (
            event_type in {"paper_exit", "unmatched_exit"}
            and isinstance(transaction_id, str)
            and transaction_id
        ):
            if transaction_id in seen_transactions:
                continue

            seen_transactions.add(transaction_id)

        unique_events.append(event)

    counts = Counter(event.get("event_type") for event in unique_events)

    total_realized_pnl = 0.0
    unknown_pnl_events = 0

    for event in unique_events:
        event_type = event.get("event_type")
        details = event.get("details") or {}

        if event_type == "paper_exit":
            pnl = details.get("total_pnl")

            if pnl is None:
                unknown_pnl_events += 1
            else:
                total_realized_pnl += float(pnl)

        elif event_type == "unmatched_exit":
            pnl = details.get("pnl")

            if pnl is None:
                unknown_pnl_events += 1
            else:
                total_realized_pnl += float(pnl)

    return {
        "total_events": len(unique_events),
        "paper_entries": counts["paper_entry"],
        "paper_exits": counts["paper_exit"],
        "unmatched_exits": counts["unmatched_exit"],
        "sessions_started": counts["session_start"],
        "sessions_stopped": counts["session_stop"],
        "runtime_errors": counts["runtime_error"],
        "realized_pnl": round(total_realized_pnl, 4),
        "unknown_pnl_events": unknown_pnl_events,
    }


def main() -> None:
    all_events = load_events()
    events = filter_session_events(all_events)
    summary = summarize(events)

    print("\n========== PAPER-TRADING REPORT ==========\n")
    print(f"Total events:       {summary['total_events']}")
    print(f"Paper entries:      {summary['paper_entries']}")
    print(f"Paper exits:        {summary['paper_exits']}")
    print(f"Unmatched exits:    {summary['unmatched_exits']}")
    print(f"Sessions started:   {summary['sessions_started']}")
    print(f"Sessions stopped:   {summary['sessions_stopped']}")
    print(f"Runtime errors:     {summary['runtime_errors']}")
    print(f"Unknown P&L events: {summary['unknown_pnl_events']}")
    print("------------------------------------------")
    print(f"Logged realized P&L: ${summary['realized_pnl']:.4f}")
    print("\n==========================================\n")


if __name__ == "__main__":
    main()
