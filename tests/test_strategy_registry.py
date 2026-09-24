from trader_api_examples.strategies import available_strategy_names, get_strategy


def test_builtin_strategies_are_independently_registered() -> None:
    assert available_strategy_names() == ("rsi", "ema_cross", "sma_cross", "macd")
    assert get_strategy("rsi").evaluate_latest.__module__.endswith("strategies.rsi")
    assert get_strategy("ema_cross").evaluate_latest.__module__.endswith("strategies.ema_cross")
    assert get_strategy("sma_cross").evaluate_latest.__module__.endswith("strategies.sma_cross")
    assert get_strategy("macd").evaluate_latest.__module__.endswith("strategies.macd")
