from __future__ import annotations

from collections.abc import Mapping

from . import ema_cross, macd, rsi, sma_cross
from .types import StrategyDefinition

_BUILTIN_STRATEGIES: dict[str, StrategyDefinition] = {
    "rsi": StrategyDefinition("rsi", rsi.required_completed_bars, rsi.evaluate_latest),
    "ema_cross": StrategyDefinition(
        "ema_cross", ema_cross.required_completed_bars, ema_cross.evaluate_latest
    ),
    "sma_cross": StrategyDefinition(
        "sma_cross", sma_cross.required_completed_bars, sma_cross.evaluate_latest
    ),
    "macd": StrategyDefinition("macd", macd.required_completed_bars, macd.evaluate_latest),
}

_strategies: dict[str, StrategyDefinition] = dict(_BUILTIN_STRATEGIES)


def available_strategy_names() -> tuple[str, ...]:
    return tuple(_strategies)


def get_strategy(name: str) -> StrategyDefinition:
    try:
        return _strategies[name]
    except KeyError as error:
        supported = ", ".join(available_strategy_names())
        raise ValueError(
            f"Unsupported strategy: {name}. Available strategies: {supported}"
        ) from error


def register_strategy(definition: StrategyDefinition, *, replace: bool = False) -> None:
    if not definition.name.strip():
        raise ValueError("Strategy name must not be empty.")
    if definition.name in _strategies and not replace:
        raise ValueError(f"Strategy is already registered: {definition.name}")
    _strategies[definition.name] = definition


def strategy_registry() -> Mapping[str, StrategyDefinition]:
    return _strategies.copy()
