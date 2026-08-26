from __future__ import annotations

from enum import StrEnum


class PositionSide(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Signal(StrEnum):
    NONE = "NONE"
    OPEN_BUY = "OPEN_BUY"
    OPEN_SELL = "OPEN_SELL"
    CLOSE_BUY = "CLOSE_BUY"
    CLOSE_SELL = "CLOSE_SELL"


def signal_from_rsi_crossing(
    previous_rsi: float,
    current_rsi: float,
    position_side: PositionSide | None,
    *,
    oversold: float = 30.0,
    overbought: float = 70.0,
    exit_level: float = 50.0,
) -> Signal:
    if position_side is PositionSide.LONG:
        return Signal.CLOSE_BUY if previous_rsi <= exit_level < current_rsi else Signal.NONE
    if position_side is PositionSide.SHORT:
        return Signal.CLOSE_SELL if previous_rsi >= exit_level > current_rsi else Signal.NONE
    if previous_rsi < oversold <= current_rsi:
        return Signal.OPEN_BUY
    if previous_rsi > overbought >= current_rsi:
        return Signal.OPEN_SELL
    return Signal.NONE
