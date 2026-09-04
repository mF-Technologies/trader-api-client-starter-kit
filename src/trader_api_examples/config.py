from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values


class ConfigError(ValueError):
    """Raised when local configuration is missing or unsafe."""


SENSITIVE_KEYS = {
    "api_key",
    "password",
    "token",
    "access_token",
    "username",
    "trade_key",
    "valid_generated_token",
}


@dataclass(frozen=True)
class EndpointsConfig:
    web_proxy_url: str = ""
    fxserver_rest_url: str = ""
    chart_server_url: str = ""
    fxserver_ws_url: str = ""
    price_agent_ws_url: str = ""


@dataclass(frozen=True)
class TradingConfig:
    contract: str = "EURUSD"
    amount: float = 1000.0
    max_runtime_seconds: int = 600
    poll_seconds: float = 5.0
    bar_count: int = 200


@dataclass(frozen=True)
class StrategyConfig:
    name: str = "rsi"
    period_type: int = 1
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    exit_level: float = 50.0
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9


@dataclass(frozen=True)
class AlgoInstanceConfig:
    name: str
    trading: TradingConfig
    strategy: StrategyConfig


@dataclass(frozen=True)
class Secrets:
    api_key: str
    username: str = ""
    trade_key: str = ""


@dataclass(frozen=True)
class AppConfig:
    environment: str
    endpoints: EndpointsConfig = field(default_factory=EndpointsConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    instances: tuple[AlgoInstanceConfig, ...] = ()
    secrets: Secrets = field(default_factory=lambda: Secrets(api_key=""))
    live_trading_enabled: bool = False

    @property
    def algo_instances(self) -> tuple[AlgoInstanceConfig, ...]:
        if self.instances:
            return self.instances
        return (AlgoInstanceConfig("default", self.trading, self.strategy),)


def _reject_sensitive_fields(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key).lower()
            if key in SENSITIVE_KEYS or key.endswith("_token") or key.endswith("_password"):
                raise ConfigError(f"Sensitive field '{raw_key}' is not allowed in YAML ({path}).")
            _reject_sensitive_fields(nested, f"{path}.{raw_key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_sensitive_fields(nested, f"{path}[{index}]")


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping.")
    return {str(key): item for key, item in value.items()}


def _environment_value(local: dict[str, str | None], name: str) -> str:
    value = os.environ[name] if name in os.environ else local.get(name)
    return (value or "").strip()


def load_config(path: Path, *, require_api_key: bool = True) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"Configuration file not found: {path}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"Configuration file is invalid YAML: {path}") from error

    data = _mapping(raw, "config")
    _reject_sensitive_fields(data)

    local_environment = dict(dotenv_values(path.parent / ".env.local", encoding="utf-8"))
    api_key = _environment_value(local_environment, "TRADER_API_KEY")
    if require_api_key and not api_key:
        raise ConfigError("Required environment variable is not set: TRADER_API_KEY")

    endpoints = _mapping(data.get("endpoints"), "endpoints")
    trading = _mapping(data.get("trading"), "trading")
    strategy = _mapping(data.get("strategy"), "strategy")
    raw_instances = data.get("instances", [])
    if not isinstance(raw_instances, list):
        raise ConfigError("instances must be a list.")

    environment = str(data.get("environment", "")).strip().lower()
    if environment not in {"demo", "live"}:
        raise ConfigError("environment must be 'demo' or 'live'.")

    default_trading = TradingConfig(**trading)
    default_strategy = StrategyConfig(**strategy)
    instances: list[AlgoInstanceConfig] = []
    for index, raw_instance in enumerate(raw_instances):
        instance = _mapping(raw_instance, f"instances[{index}]")
        name = str(instance.get("name", "")).strip()
        if not name:
            raise ConfigError(f"instances[{index}].name must not be empty.")
        trading_overrides = _mapping(instance.get("trading"), f"instances[{index}].trading")
        strategy_overrides = _mapping(instance.get("strategy"), f"instances[{index}].strategy")
        instances.append(
            AlgoInstanceConfig(
                name=name,
                trading=TradingConfig(**({**vars(default_trading), **trading_overrides})),
                strategy=StrategyConfig(**({**vars(default_strategy), **strategy_overrides})),
            )
        )

    result = AppConfig(
        environment=environment,
        endpoints=EndpointsConfig(**endpoints),
        trading=default_trading,
        strategy=default_strategy,
        instances=tuple(instances),
        secrets=Secrets(
            api_key=api_key,
            username=_environment_value(local_environment, "TRADER_API_USERNAME"),
            trade_key=_environment_value(local_environment, "TRADER_API_TRADE_KEY"),
        ),
        live_trading_enabled=(
            _environment_value(local_environment, "TRADER_API_ENABLE_LIVE_TRADING").lower()
            == "true"
        ),
    )
    _validate_config(result)
    return result


def _validate_config(config: AppConfig) -> None:
    names = [instance.name for instance in config.algo_instances]
    if len(names) != len(set(names)):
        raise ConfigError("instances.name values must be unique.")
    for instance in config.algo_instances:
        trading = instance.trading
        strategy = instance.strategy
        prefix = f"instances[{instance.name}]"
        if not trading.contract.strip():
            raise ConfigError(f"{prefix}.trading.contract must not be empty.")
        if trading.amount <= 0:
            raise ConfigError(f"{prefix}.trading.amount must be greater than zero.")
        if trading.max_runtime_seconds <= 0:
            raise ConfigError(f"{prefix}.trading.max_runtime_seconds must be greater than zero.")
        if trading.poll_seconds <= 0:
            raise ConfigError(f"{prefix}.trading.poll_seconds must be greater than zero.")
        if strategy.name not in {"rsi", "macd", "ema_cross"}:
            raise ConfigError(f"{prefix}.strategy.name is not supported: {strategy.name}")
        if strategy.period_type not in range(1, 21):
            raise ConfigError(f"{prefix}.strategy.period_type is not supported.")
        if strategy.name == "rsi":
            required_bars = strategy.rsi_period + 2
        elif strategy.name == "ema_cross":
            required_bars = strategy.slow_period + 2
        else:
            required_bars = strategy.slow_period + strategy.signal_period + 2
        if trading.bar_count < required_bars:
            raise ConfigError(f"{prefix}.trading.bar_count is too small for the strategy.")
        if strategy.name in {"macd", "ema_cross"} and (
            strategy.fast_period <= 0 or strategy.slow_period <= strategy.fast_period
        ):
            raise ConfigError(f"{prefix}.strategy periods require 0 < fast_period < slow_period.")
        if strategy.name == "macd" and strategy.signal_period <= 0:
            raise ConfigError(f"{prefix}.strategy.signal_period must be greater than zero.")
        if strategy.name == "rsi" and not 0 < strategy.oversold < strategy.exit_level:
            raise ConfigError(f"{prefix}.strategy.oversold must be below exit_level.")
        if strategy.name == "rsi" and not strategy.exit_level < strategy.overbought < 100:
            raise ConfigError(f"{prefix}.strategy.overbought must be above exit_level.")
