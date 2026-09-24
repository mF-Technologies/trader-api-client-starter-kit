from __future__ import annotations

import numpy as np
from numpy.typing import NDArray

from ..api import Bar
from ..strategy import PositionSide, Signal, signal_from_line_crossing
from .types import SignalEvent


def line_cross_event(
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
