from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import talib

from ..api import Bar
from ..strategy import PositionSide
from .common import line_cross_event
from .types import StrategyEvaluation

if TYPE_CHECKING:
    from ..config import StrategyConfig


def required_completed_bars(strategy: StrategyConfig) -> int:
    return strategy.slow_period + 2


def evaluate_latest(
    bars: list[Bar],
    *,
    strategy: StrategyConfig,
    position_side: PositionSide | None,
) -> StrategyEvaluation:
    if len(bars) < required_completed_bars(strategy):
        return StrategyEvaluation(None, {})
    closes = np.asarray([bar.close for bar in bars], dtype=np.float64)
    fast = talib.EMA(closes, timeperiod=strategy.fast_period)
    slow = talib.EMA(closes, timeperiod=strategy.slow_period)
    event = line_cross_event(
        bars,
        fast,
        slow,
        strategy=strategy.name,
        position_side=position_side,
    )
    return StrategyEvaluation(
        event,
        {
            "ema_fast": round(float(fast[-1]), 6),
            "ema_slow": round(float(slow[-1]), 6),
        },
    )
