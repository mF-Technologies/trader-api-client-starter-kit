from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
    period_type: int = 1
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    exit_level: float = 50.0


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
    secrets: Secrets = field(default_factory=lambda: Secrets(api_key=""))


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


def load_config(path: Path, *, require_api_key: bool = True) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigError(f"Configuration file not found: {path}") from error
    except yaml.YAMLError as error:
        raise ConfigError(f"Configuration file is invalid YAML: {path}") from error

    data = _mapping(raw, "config")
    _reject_sensitive_fields(data)

    api_key = os.getenv("TRADER_API_KEY", "").strip()
    if require_api_key and not api_key:
        raise ConfigError("Required environment variable is not set: TRADER_API_KEY")

    endpoints = _mapping(data.get("endpoints"), "endpoints")
    trading = _mapping(data.get("trading"), "trading")
    strategy = _mapping(data.get("strategy"), "strategy")

    environment = str(data.get("environment", "")).strip().lower()
    if environment not in {"demo", "live"}:
        raise ConfigError("environment must be 'demo' or 'live'.")

    result = AppConfig(
        environment=environment,
        endpoints=EndpointsConfig(**endpoints),
        trading=TradingConfig(**trading),
        strategy=StrategyConfig(**strategy),
        secrets=Secrets(
            api_key=api_key,
            username=os.getenv("TRADER_API_USERNAME", "").strip(),
            trade_key=os.getenv("TRADER_API_TRADE_KEY", "").strip(),
        ),
    )
    _validate_config(result)
    return result


def _validate_config(config: AppConfig) -> None:
    if config.trading.amount <= 0:
        raise ConfigError("trading.amount must be greater than zero.")
    if config.trading.max_runtime_seconds <= 0:
        raise ConfigError("trading.max_runtime_seconds must be greater than zero.")
    if config.trading.bar_count < config.strategy.rsi_period + 2:
        raise ConfigError("trading.bar_count must provide enough completed bars for RSI.")
    if not 0 < config.strategy.oversold < config.strategy.exit_level:
        raise ConfigError("strategy.oversold must be below strategy.exit_level.")
    if not config.strategy.exit_level < config.strategy.overbought < 100:
        raise ConfigError("strategy.overbought must be above strategy.exit_level.")
