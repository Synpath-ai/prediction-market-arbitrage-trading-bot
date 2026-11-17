
from trading.report import filter_session_events, summarize


def test_filter_excludes_events_outside_sessions():
    events = [
        {"event_type": "paper_entry"},
        {"event_type": "session_start"},
        {"event_type": "paper_entry"},
        {"event_type": "session_stop"},
        {"event_type": "paper_exit"},
    ]

    filtered = filter_session_events(events)

    assert [event["event_type"] for event in filtered] == [
        "session_start",
        "paper_entry",
        "session_stop",
    ]


def test_filter_keeps_active_session_without_stop():
    events = [
        {"event_type": "session_start"},
        {"event_type": "paper_entry"},
        {"event_type": "paper_exit"},
    ]

    filtered = filter_session_events(events)

    assert len(filtered) == 3


def test_summarize_adds_matched_and_unmatched_pnl():
    events = [
        {
            "event_type": "paper_exit",
            "details": {"total_pnl": 2.50},
        },
        {
            "event_type": "unmatched_exit",
            "details": {"pnl": -0.75},
        },
    ]

    summary = summarize(events)

    assert summary["realized_pnl"] == 1.75


def test_summarize_identifies_unknown_unmatched_pnl():
    events = [
        {
            "event_type": "paper_exit",
            "details": {"total_pnl": 2.50},
        },
        {
            "event_type": "unmatched_exit",
            "details": {"pnl": None},
        },
    ]

    summary = summarize(events)

    assert summary["realized_pnl"] == 2.50
    assert summary["unknown_pnl_events"] == 1


def test_summarize_counts_sessions_and_errors():
    events = [
        {"event_type": "session_start"},
        {"event_type": "paper_entry"},
        {"event_type": "runtime_error"},
        {"event_type": "session_stop"},
    ]

    summary = summarize(events)

    assert summary["sessions_started"] == 1
    assert summary["sessions_stopped"] == 1
    assert summary["paper_entries"] == 1
    assert summary["runtime_errors"] == 1

def test_summarize_deduplicates_replayed_transaction_events():
    events = [
        {
            "event_type": "paper_exit",
            "details": {
                "transaction_id": "matched-001",
                "total_pnl": 2.50,
            },
        },
        {
            "event_type": "paper_exit",
            "details": {
                "transaction_id": "matched-001",
                "total_pnl": 2.50,
            },
        },
        {
            "event_type": "unmatched_exit",
            "details": {
                "transaction_id": "unmatched-001",
                "pnl": -0.75,
            },
        },
        {
            "event_type": "unmatched_exit",
            "details": {
                "transaction_id": "unmatched-001",
                "pnl": -0.75,
            },
        },
    ]

    summary = summarize(events)

    assert summary["total_events"] == 2
    assert summary["paper_exits"] == 1
    assert summary["unmatched_exits"] == 1
    assert summary["realized_pnl"] == 1.75