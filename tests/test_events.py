import pytest
import json
from datetime import datetime

from trading.events import EventLogger


def test_log_creates_directory_and_writes_event(tmp_path):
    log_path = tmp_path / "state" / "events.jsonl"
    logger = EventLogger(log_path)

    logger.log(
        "position_opened",
        market="TEST-MARKET",
        details={"contracts": 5, "price": 0.45},
    )

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    event = json.loads(lines[0])

    assert event["event_type"] == "position_opened"
    assert event["market"] == "TEST-MARKET"
    assert event["details"] == {"contracts": 5, "price": 0.45}
    datetime.fromisoformat(event["timestamp"])


def test_log_appends_multiple_events(tmp_path):
    logger = EventLogger(tmp_path / "events.jsonl")

    logger.log("position_opened", market="MARKET-A")
    logger.log("position_closed", market="MARKET-A")

    lines = (tmp_path / "events.jsonl").read_text(
        encoding="utf-8"
    ).splitlines()

    assert len(lines) == 2
    assert json.loads(lines[0])["event_type"] == "position_opened"
    assert json.loads(lines[1])["event_type"] == "position_closed"


def test_log_supports_optional_market_and_details(tmp_path):
    logger = EventLogger(tmp_path / "events.jsonl")

    logger.log("heartbeat")

    event = json.loads(
        (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    )

    assert event["event_type"] == "heartbeat"
    assert event["market"] is None
    assert event["details"] == {}

def test_log_io_error_warns_without_crashing(tmp_path, capsys):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("This is a file, not a directory.")

    logger = EventLogger(blocked_parent / "events.jsonl")

    logger.log("heartbeat")

    captured = capsys.readouterr()

    assert "[EVENT LOG ERROR]" in captured.err
    assert "heartbeat" in captured.err

def test_log_can_propagate_io_errors_when_transactional(tmp_path):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text(
        "This is a file, not a directory.",
        encoding="utf-8",
    )

    logger = EventLogger(blocked_parent / "events.jsonl")

    with pytest.raises(OSError):
        logger.log(
            "paper_exit",
            raise_on_error=True,
        )