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
    return strategy.slow_period + strategy.signal_period + 2


def evaluate_latest(
    bars: list[Bar],
    *,
    strategy: StrategyConfig,
    position_side: PositionSide | None,
) -> StrategyEvaluation:
    if len(bars) < required_completed_bars(strategy):
        return StrategyEvaluation(None, {})
    closes = np.asarray([bar.close for bar in bars], dtype=np.float64)
    macd, signal_line, histogram = talib.MACD(
        closes,
        fastperiod=strategy.fast_period,
        slowperiod=strategy.slow_period,
        signalperiod=strategy.signal_period,
    )
    event = line_cross_event(
        bars,
        macd,
        signal_line,
        strategy=strategy.name,
        position_side=position_side,
    )
    return StrategyEvaluation(
        event,
        {
            "macd": round(float(macd[-1]), 6),
            "signal_line": round(float(signal_line[-1]), 6),
            "histogram": round(float(histogram[-1]), 6),
        },
    )
