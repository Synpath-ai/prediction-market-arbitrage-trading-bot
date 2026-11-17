
"""Structured event logging for the paper-trading bot."""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class EventLogger:
    """Append structured events to a JSON Lines file."""

    def __init__(self, path: str | Path = "state/events.jsonl"):
        self.path = Path(path)

    def log(
        self,
        event_type: str,
        *,
        market: str | None = None,
        details: dict[str, Any] | None = None,
        raise_on_error: bool = False,
    ) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "market": market,
            "details": details or {},
        }

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)

            with self.path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event, default=str) + "\n")

        except OSError as exc:
            print(
                f"[EVENT LOG ERROR] Could not write {event_type}: {exc}",
                file=sys.stderr,
            )

            if raise_on_error:
                raise
