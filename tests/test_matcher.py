from types import SimpleNamespace

import pytest

from src.matcher import Pair, resolve, tradeable


def make_pair(rules):
    return Pair(
        event_id="test-event",
        event="Test Event",
        outcome="Test Outcome",
        kalshi_id="kalshi:TEST",
        poly_id="polymarket:123",
        flipped=False,
        rules=rules,
    )


def test_same_rules_are_always_tradeable():
    pair = make_pair("same")

    assert tradeable(pair)
    assert tradeable(pair, require_same=False)


def test_insufficient_rules_require_explicit_permission():
    pair = make_pair("insufficient")

    assert not tradeable(pair)
    assert tradeable(pair, require_same=False)


@pytest.mark.parametrize(
    "rules",
    [
        "not_same",
        "unknown",
    ],
)
def test_mismatched_or_unknown_rules_are_never_tradeable(rules):
    pair = make_pair(rules)

    assert not tradeable(pair)
    assert not tradeable(pair, require_same=False)

def test_resolve_normalizes_valid_kalshi_market_id(monkeypatch):
    captured = {}

    link = SimpleNamespace(
        venue="polymarket",
        id="polymarket:123",
        side_map={"yes": "yes"},
    )

    def fake_match_market(market_id, *, venue):
        captured["market_id"] = market_id
        captured["venue"] = venue
        return SimpleNamespace(matched=link)

    monkeypatch.setattr(
        "src.matcher.synpath.match_market",
        fake_match_market,
    )

    event_data = {
        "title": " Test Event ",
        "outcomes": [
            {
                "label": "Test Outcome",
                "contracts": [
                    {
                        "venue": "kalshi",
                        "native_market_id": "TEST",
                        "contract_id": "k1",
                    },
                    {
                        "venue": "polymarket",
                        "native_market_id": "123",
                        "contract_id": "p1",
                    },
                ],
                "comparisons": [
                    {
                        "left_contract_id": "k1",
                        "right_contract_id": "p1",
                        "internal_state": "same",
                        "reason": "Settlement rules match",
                    }
                ],
            }
        ],
    }

    class FakeResponse:
        def raise_for_status(self):
            return self

        def json(self):
            return event_data

    class FakeHttp:
        def get(self, url):
            captured["url"] = url
            return FakeResponse()

    pair = resolve(
        "event-1",
        "kalshi:TEST",
        http=FakeHttp(),
    )

    assert captured["market_id"] == "kalshi:TEST"
    assert captured["venue"] == "polymarket"
    assert captured["url"].endswith("/events/event-1")
    assert pair.kalshi_id == "kalshi:TEST"
    assert pair.poly_id == "polymarket:123"
    assert pair.rules == "same"
    assert pair.flipped is False


@pytest.mark.parametrize(
    "market_id",
    [
        "",
        "TEST",
        "polymarket:TEST",
        "kalshi:",
        "kalshi:TEST:EXTRA",
    ],
)
def test_resolve_rejects_invalid_kalshi_market_ids(
    monkeypatch, market_id
):
    monkeypatch.setattr(
        "src.matcher.synpath.match_market",
        lambda *args, **kwargs: pytest.fail(
            "Synpath should not be called for an invalid market ID"
        ),
    )

    with pytest.raises(ValueError, match="Kalshi market ID"):
        resolve("event-1", market_id)