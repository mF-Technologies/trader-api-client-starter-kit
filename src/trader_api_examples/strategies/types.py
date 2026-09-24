from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..api import Bar
from ..strategy import PositionSide, Signal

if TYPE_CHECKING:
    from ..config import StrategyConfig


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
class StrategyEvaluation:
    event: SignalEvent | None
    indicators: dict[str, float]


class RequiredBars(Protocol):
    def __call__(self, strategy: StrategyConfig) -> int: ...


class StrategyEvaluator(Protocol):
    def __call__(
        self,
        bars: list[Bar],
        *,
        strategy: StrategyConfig,
        position_side: PositionSide | None,
    ) -> StrategyEvaluation: ...


@dataclass(frozen=True)
class StrategyDefinition:
    name: str
    required_completed_bars: RequiredBars
    evaluate_latest: StrategyEvaluator
