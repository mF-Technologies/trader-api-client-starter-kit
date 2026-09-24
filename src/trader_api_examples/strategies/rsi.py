from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import talib
from numpy.typing import NDArray

from ..api import Bar
from ..strategy import PositionSide, Signal, signal_from_rsi_crossing
from .types import SignalEvent, StrategyEvaluation

if TYPE_CHECKING:
    from ..config import StrategyConfig


def calculate_rsi(bars: list[Bar], period: int) -> NDArray[np.float64]:
    closes = np.asarray([bar.close for bar in bars], dtype=np.float64)
    return talib.RSI(closes, timeperiod=period)


def required_completed_bars(strategy: StrategyConfig) -> int:
    return strategy.rsi_period + 2


def evaluate_latest(
    bars: list[Bar],
    *,
    strategy: StrategyConfig,
    position_side: PositionSide | None,
) -> StrategyEvaluation:
    if len(bars) < required_completed_bars(strategy):
        return StrategyEvaluation(None, {})
    values = calculate_rsi(bars, strategy.rsi_period)
    previous, current = float(values[-2]), float(values[-1])
    if np.isnan(previous) or np.isnan(current):
        return StrategyEvaluation(None, {})
    signal = signal_from_rsi_crossing(
        previous,
        current,
        position_side,
        oversold=strategy.oversold,
        overbought=strategy.overbought,
        exit_level=strategy.exit_level,
    )
    event = (
        SignalEvent(
            bars[-1].time_ms,
            signal,
            round(current, 2),
            bars[-1].close,
            strategy.name,
        )
        if signal is not Signal.NONE
        else None
    )
    return StrategyEvaluation(event, {"rsi": round(current, 2)})
