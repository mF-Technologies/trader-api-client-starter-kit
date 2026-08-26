from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import talib

from .api import Bar
from .strategy import PositionSide, Signal, signal_from_rsi_crossing


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
    rsi: float
    close: float


@dataclass(frozen=True)
class AlgoResult:
    outcome: Outcome
    events: list[SignalEvent]
    message: str


def calculate_rsi(bars: list[Bar], period: int) -> np.ndarray:
    closes = np.asarray([bar.close for bar in bars], dtype=np.float64)
    return talib.RSI(closes, timeperiod=period)


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
        rsi=round(current, 2),
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
                rsi=round(current, 2),
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
