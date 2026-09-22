from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import talib
from numpy.typing import NDArray

from .api import Bar
from .config import StrategyConfig
from .strategy import PositionSide, Signal, signal_from_line_crossing, signal_from_rsi_crossing


class Outcome(StrEnum):
    SUCCESS = "SUCCESS"
    NO_SIGNAL = "NO_SIGNAL"
    INCONCLUSIVE = "INCONCLUSIVE"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"


@dataclass(frozen=True)
class SignalEvent:
    time_ms: int
    signal: Signal
    indicator_value: float
    close: float
    strategy: str = "rsi"

    @property
    def rsi(self) -> float:
        return self.indicator_value


@dataclass(frozen=True)
class AlgoResult:
    outcome: Outcome
    events: list[SignalEvent]
    message: str


@dataclass(frozen=True)
class StrategyEvaluation:
    event: SignalEvent | None
    indicators: dict[str, float]


def calculate_rsi(bars: list[Bar], period: int) -> NDArray[np.float64]:
    closes = np.asarray([bar.close for bar in bars], dtype=np.float64)
    return talib.RSI(closes, timeperiod=period)


def required_completed_bars(strategy: StrategyConfig) -> int:
    if strategy.name == "rsi":
        return strategy.rsi_period + 2
    if strategy.name == "ema_cross":
        return strategy.slow_period + 2
    if strategy.name == "macd":
        return strategy.slow_period + strategy.signal_period + 2
    raise ValueError(f"Unsupported strategy: {strategy.name}")


def _line_cross_event(
    bars: list[Bar],
    first: NDArray[np.float64],
    second: NDArray[np.float64],
    *,
    strategy: str,
    position_side: PositionSide | None,
) -> SignalEvent | None:
    previous = float(first[-2] - second[-2])
    current = float(first[-1] - second[-1])
    if np.isnan(previous) or np.isnan(current):
        return None
    signal = signal_from_line_crossing(previous, current, position_side)
    if signal is Signal.NONE:
        return None
    return SignalEvent(
        time_ms=bars[-1].time_ms,
        signal=signal,
        indicator_value=round(current, 6),
        close=bars[-1].close,
        strategy=strategy,
    )


def evaluate_latest_strategy(
    bars: list[Bar],
    *,
    strategy: StrategyConfig,
    position_side: PositionSide | None,
) -> StrategyEvaluation:
    if len(bars) < required_completed_bars(strategy):
        return StrategyEvaluation(None, {})
    closes = np.asarray([bar.close for bar in bars], dtype=np.float64)
    if strategy.name == "rsi":
        values = talib.RSI(closes, timeperiod=strategy.rsi_period)
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
    if strategy.name == "ema_cross":
        fast = talib.EMA(closes, timeperiod=strategy.fast_period)
        slow = talib.EMA(closes, timeperiod=strategy.slow_period)
        event = _line_cross_event(
            bars, fast, slow, strategy=strategy.name, position_side=position_side
        )
        return StrategyEvaluation(
            event,
            {
                "ema_fast": round(float(fast[-1]), 6),
                "ema_slow": round(float(slow[-1]), 6),
            },
        )
    if strategy.name == "macd":
        macd, signal_line, histogram = talib.MACD(
            closes,
            fastperiod=strategy.fast_period,
            slowperiod=strategy.slow_period,
            signalperiod=strategy.signal_period,
        )
        event = _line_cross_event(
            bars, macd, signal_line, strategy=strategy.name, position_side=position_side
        )
        return StrategyEvaluation(
            event,
            {
                "macd": round(float(macd[-1]), 6),
                "signal_line": round(float(signal_line[-1]), 6),
                "histogram": round(float(histogram[-1]), 6),
            },
        )
    raise ValueError(f"Unsupported strategy: {strategy.name}")


def latest_strategy_signal(
    bars: list[Bar],
    *,
    strategy: StrategyConfig,
    position_side: PositionSide | None,
) -> SignalEvent | None:
    return evaluate_latest_strategy(bars, strategy=strategy, position_side=position_side).event


def latest_signal(
    bars: list[Bar],
    *,
    rsi_period: int,
    position_side: PositionSide | None,
    oversold: float = 30.0,
    overbought: float = 70.0,
    exit_level: float = 50.0,
) -> SignalEvent | None:
    if len(bars) < rsi_period + 2:
        return None
    values = calculate_rsi(bars, rsi_period)
    previous = float(values[-2])
    current = float(values[-1])
    if np.isnan(previous) or np.isnan(current):
        return None
    signal = signal_from_rsi_crossing(
        previous,
        current,
        position_side,
        oversold=oversold,
        overbought=overbought,
        exit_level=exit_level,
    )
    if signal is Signal.NONE:
        return None
    return SignalEvent(
        time_ms=bars[-1].time_ms,
        signal=signal,
        indicator_value=round(current, 2),
        close=bars[-1].close,
    )


def evaluate_replay(
    bars: list[Bar],
    *,
    rsi_period: int,
    oversold: float = 30.0,
    overbought: float = 70.0,
    exit_level: float = 50.0,
) -> AlgoResult:
    if len(bars) < rsi_period + 2:
        return AlgoResult(Outcome.INCONCLUSIVE, [], "Not enough completed bars for RSI.")

    rsi_values = calculate_rsi(bars, rsi_period)
    events: list[SignalEvent] = []
    position: PositionSide | None = None
    opened = False

    for index in range(1, len(bars)):
        previous = float(rsi_values[index - 1])
        current = float(rsi_values[index])
        if np.isnan(previous) or np.isnan(current):
            continue
        signal = signal_from_rsi_crossing(
            previous,
            current,
            position,
            oversold=oversold,
            overbought=overbought,
            exit_level=exit_level,
        )
        if signal is Signal.NONE:
            continue
        events.append(
            SignalEvent(
                time_ms=bars[index].time_ms,
                signal=signal,
                indicator_value=round(current, 2),
                close=bars[index].close,
            )
        )
        if signal is Signal.OPEN_BUY:
            position = PositionSide.LONG
            opened = True
        elif signal is Signal.OPEN_SELL:
            position = PositionSide.SHORT
            opened = True
        elif signal in {Signal.CLOSE_BUY, Signal.CLOSE_SELL}:
            position = None
            break

    if not opened:
        return AlgoResult(Outcome.NO_SIGNAL, [], "No RSI crossing occurred.")
    message = (
        "RSI replay completed one round trip."
        if position is None
        else "RSI replay ended with a proposed open position."
    )
    return AlgoResult(Outcome.SUCCESS, events, message)
