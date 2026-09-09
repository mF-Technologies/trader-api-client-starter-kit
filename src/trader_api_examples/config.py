from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime
from math import isfinite
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
    contract: str = "LLG"
    amount: float = 1000.0
    # API amount represented by one lot when converting lot-based replay costs.
    amount_per_lot: float = 1.0
    # Applies while flat and waiting for a new entry. An open trade uses max_holding_hours.
    max_runtime_seconds: int = 600
    max_holding_hours: float | None = 120.0
    # Live risk controls use account equity and are persisted across restarts.
    max_trade_loss_pct: float | None = 1.0
    max_daily_loss_pct: float | None = 2.0
    max_drawdown_pct: float | None = 10.0
    poll_seconds: float = 5.0
    bar_count: int = 200
    # These are the session hours observed in the uploaded LLG/XAUUSD H1 history.
    # They are configurable because the broker's schedule is an account-level input.
    market_data_daily_break_start_utc: str | None = "23:00"
    market_data_daily_break_end_utc: str | None = "01:00"
    market_data_closed_dates_utc: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyConfig:
    period_type: int = 1
    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    exit_level: float = 50.0
    sma_period_type: int = 3
    sma_fast_period: int = 20
    sma_slow_period: int = 50
    atr_period: int = 14
    sma_exit_buffer_atr: float = 0.0
    atr_stop_multiple: float = 2.0
    commission_rate: float = 0.0
    slippage_bps: float = 0.0
    commission_per_unit: float = 0.0
    commission_round_turn_per_lot: float = 0.0
    spread_bps: float = 0.0
    financing_bps_per_day: float = 0.0
    market_impact_bps: float = 0.0


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
    live_trading_enabled: bool = False


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

    closed_dates = trading.get("market_data_closed_dates_utc", ())
    if closed_dates is None:
        closed_dates = ()
    elif not isinstance(closed_dates, (list, tuple)):
        raise ConfigError("trading.market_data_closed_dates_utc must be a list.")
    trading["market_data_closed_dates_utc"] = tuple(str(value).strip() for value in closed_dates)

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
    if config.trading.amount <= 0:
        raise ConfigError("trading.amount must be greater than zero.")
    if not isfinite(config.trading.amount_per_lot) or config.trading.amount_per_lot <= 0:
        raise ConfigError("trading.amount_per_lot must be finite and greater than zero.")
    if config.trading.max_runtime_seconds <= 0:
        raise ConfigError("trading.max_runtime_seconds must be greater than zero.")
    if config.trading.max_holding_hours is not None and (
        not isfinite(config.trading.max_holding_hours) or config.trading.max_holding_hours <= 0
    ):
        raise ConfigError("trading.max_holding_hours must be finite and greater than zero.")
    for name, limit in (
        ("max_trade_loss_pct", config.trading.max_trade_loss_pct),
        ("max_daily_loss_pct", config.trading.max_daily_loss_pct),
        ("max_drawdown_pct", config.trading.max_drawdown_pct),
    ):
        if limit is not None and (not isfinite(limit) or limit <= 0 or limit > 100):
            raise ConfigError(f"trading.{name} must be between zero and 100 when set.")
    start = config.trading.market_data_daily_break_start_utc
    end = config.trading.market_data_daily_break_end_utc
    if (start is None) != (end is None):
        raise ConfigError(
            "trading.market_data_daily_break_start_utc and "
            "market_data_daily_break_end_utc must be set together."
        )
    for name, clock_value in (
        ("market_data_daily_break_start_utc", start),
        ("market_data_daily_break_end_utc", end),
    ):
        if clock_value is not None:
            try:
                datetime.strptime(clock_value, "%H:%M")
            except ValueError as error:
                raise ConfigError(f"trading.{name} must use HH:MM UTC format.") from error
    for closed_date in config.trading.market_data_closed_dates_utc:
        try:
            date.fromisoformat(closed_date)
        except ValueError as error:
            raise ConfigError(
                "trading.market_data_closed_dates_utc must contain ISO dates."
            ) from error
    if config.trading.bar_count < config.strategy.rsi_period + 2:
        raise ConfigError("trading.bar_count must provide enough completed bars for RSI.")
    if not 0 < config.strategy.oversold < config.strategy.exit_level:
        raise ConfigError("strategy.oversold must be below strategy.exit_level.")
    if not config.strategy.exit_level < config.strategy.overbought < 100:
        raise ConfigError("strategy.overbought must be above strategy.exit_level.")
    if config.strategy.sma_period_type not in {1, 2, 3}:
        raise ConfigError("strategy.sma_period_type must be 1 (minute), 2 (hourly), or 3 (daily).")
    if config.strategy.sma_fast_period <= 0:
        raise ConfigError("strategy.sma_fast_period must be greater than zero.")
    if config.strategy.sma_fast_period >= config.strategy.sma_slow_period:
        raise ConfigError("strategy.sma_fast_period must be below sma_slow_period.")
    if config.strategy.atr_period <= 0:
        raise ConfigError("strategy.atr_period must be greater than zero.")
    if config.strategy.sma_exit_buffer_atr < 0:
        raise ConfigError("strategy.sma_exit_buffer_atr must not be negative.")
    if config.strategy.atr_stop_multiple <= 0:
        raise ConfigError("strategy.atr_stop_multiple must be greater than zero.")
    if config.strategy.commission_rate < 0:
        raise ConfigError("strategy.commission_rate must not be negative.")
    if not 0 <= config.strategy.slippage_bps < 10_000:
        raise ConfigError("strategy.slippage_bps must be between zero and 10000.")
    if config.strategy.commission_per_unit < 0:
        raise ConfigError("strategy.commission_per_unit must not be negative.")
    if (
        not isfinite(config.strategy.commission_round_turn_per_lot)
        or config.strategy.commission_round_turn_per_lot < 0
    ):
        raise ConfigError(
            "strategy.commission_round_turn_per_lot must be finite and not negative."
        )
    for name, cost_value in (
        ("spread_bps", config.strategy.spread_bps),
        ("financing_bps_per_day", config.strategy.financing_bps_per_day),
        ("market_impact_bps", config.strategy.market_impact_bps),
    ):
        if not 0 <= cost_value < 10_000:
            raise ConfigError(f"strategy.{name} must be between zero and 10000.")
