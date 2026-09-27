import sys
from pathlib import Path

import pytest
from synpath import FeeSchedule

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def kalshi_fee() -> FeeSchedule:
    return FeeSchedule(venue="kalshi", scope="series", scope_id="TEST", fee_type="quadratic",
                       multiplier=1.0, rounding="up_to_cent")


@pytest.fixture
def poly_fee() -> FeeSchedule:
    return FeeSchedule(venue="polymarket", scope="market", scope_id="1", fee_type="quadratic_theta",
                       taker_rate=0.04, maker_rate=0.0, exponent=1.0)
