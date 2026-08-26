from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from .config import AppConfig, ConfigError


@dataclass(frozen=True)
class Quote:
    contract: str
    bid: float
    ask: float
    tag: str


async def read_quote(config: AppConfig, timeout_seconds: float = 10) -> Quote:
    required = {
        "endpoints.fxserver_ws_url": config.endpoints.fxserver_ws_url,
        "endpoints.price_agent_ws_url": config.endpoints.price_agent_ws_url,
        "TRADER_API_USERNAME": config.secrets.username,
        "TRADER_API_TRADE_KEY": config.secrets.trade_key,
        "TRADER_API_KEY": config.secrets.api_key,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ConfigError("Price client settings are missing: " + ", ".join(missing))
    try:
        from fxserverclientpython import FxServerClientLib
    except ImportError as error:
        raise ConfigError(
            "Install the optional price client with trader-api-examples[prices]."
        ) from error

    client = FxServerClientLib(asyncio.get_running_loop())
    client.init(
        {
            "endpoint": config.endpoints.fxserver_ws_url,
            "price_agent_endpoint": config.endpoints.price_agent_ws_url,
            "trade_key": config.secrets.trade_key,
            "webproxy_endpoint": config.endpoints.web_proxy_url,
            "username": config.secrets.username,
            "valid_generated_token": config.secrets.api_key,
        }
    )
    await client.login()
    try:
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            price: Any = client.get_price_info(config.trading.contract)
            if price is not None:
                return Quote(
                    contract=config.trading.contract,
                    bid=float(price.bid),
                    ask=float(price.ask),
                    tag=str(price.tag),
                )
            await asyncio.sleep(0.1)
        raise TimeoutError(f"No quote received for {config.trading.contract}.")
    finally:
        logout = getattr(client, "logout", None)
        if logout is not None:
            result = logout()
            if asyncio.iscoroutine(result):
                await result
