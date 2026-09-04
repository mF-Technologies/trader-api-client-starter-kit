from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .config import AppConfig, ConfigError


@dataclass(frozen=True)
class Quote:
    contract: str
    bid: float
    ask: float
    tag: str


class PriceClient(Protocol):
    def init(self, config: dict[str, str]) -> None: ...
    async def login(self) -> Any: ...
    def get_price_info(self, contract: str) -> Any: ...


class PriceStreamSession:
    def __init__(
        self,
        config: AppConfig,
        *,
        client_factory: Callable[[], PriceClient] | None = None,
    ) -> None:
        self.config = config
        self._client_factory = client_factory
        self._client: PriceClient | None = None
        self._price_listener_id: str | None = None
        self._last_updates: dict[str, float] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._previous_exception_handler: (
            Callable[[asyncio.AbstractEventLoop, dict[str, Any]], object] | None
        ) = None
        self._background_error: BaseException | None = None

    async def __aenter__(self) -> PriceStreamSession:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def _validate(self) -> None:
        required = {
            "endpoints.fxserver_ws_url": self.config.endpoints.fxserver_ws_url,
            "endpoints.price_agent_ws_url": self.config.endpoints.price_agent_ws_url,
            "TRADER_API_USERNAME": self.config.secrets.username,
            "TRADER_API_TRADE_KEY": self.config.secrets.trade_key,
            "TRADER_API_KEY": self.config.secrets.api_key,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigError("Price client settings are missing: " + ", ".join(missing))

    def _make_client(self) -> PriceClient:
        if self._client_factory is not None:
            return self._client_factory()
        try:
            from fxserverclientpython import FxServerClientLib
        except ImportError as error:
            raise ConfigError(
                "Install the optional price client with trader-api-examples[prices]."
            ) from error
        return FxServerClientLib(asyncio.get_running_loop())  # type: ignore[no-any-return]

    async def connect(self) -> None:
        self._validate()
        if self._client is not None:
            return
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._previous_exception_handler = loop.get_exception_handler()
        self._background_error = None
        loop.set_exception_handler(self._handle_loop_exception)
        try:
            client = self._make_client()
            client.init(
                {
                    "endpoint": self.config.endpoints.fxserver_ws_url,
                    "price_agent_endpoint": self.config.endpoints.price_agent_ws_url,
                    "trade_key": self.config.secrets.trade_key,
                    "webproxy_endpoint": self.config.endpoints.web_proxy_url,
                    "username": self.config.secrets.username,
                    "valid_generated_token": self.config.secrets.api_key,
                }
            )
            await client.login()
        except BaseException:
            self._restore_loop_exception_handler()
            raise
        self._client = client
        self._last_updates.clear()
        add_listener = getattr(client, "add_price_listener", None)
        try:
            if add_listener is not None:
                self._price_listener_id = str(add_listener(self._record_price_update))
        except Exception:
            self._price_listener_id = None

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            self._restore_loop_exception_handler()
            return
        # fxserverclientpython 0.1.10 can crash its native job runner during logout.
        # Event-loop shutdown closes this process-scoped transport instead.
        self._price_listener_id = None
        self._last_updates.clear()
        await asyncio.sleep(0)
        self._restore_loop_exception_handler()

    def _record_price_update(self, event: Any) -> None:
        received_at = time.monotonic()
        for contract in getattr(event, "contract_codes", ()):
            self._last_updates[str(contract).strip().upper()] = received_at

    def _handle_loop_exception(
        self, loop: asyncio.AbstractEventLoop, context: dict[str, Any]
    ) -> None:
        exception = context.get("exception")
        if isinstance(exception, RuntimeError) and str(exception) == "Client is not connected":
            self._background_error = exception
            return
        if self._previous_exception_handler is not None:
            self._previous_exception_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    def _restore_loop_exception_handler(self) -> None:
        if (
            self._loop is not None
            and self._loop.get_exception_handler() == self._handle_loop_exception
        ):
            self._loop.set_exception_handler(self._previous_exception_handler)
        self._loop = None
        self._previous_exception_handler = None

    def _raise_background_error(self) -> None:
        if self._background_error is not None:
            raise TimeoutError(
                "Price transport background task disconnected; restart the process."
            ) from self._background_error

    async def get_quote(self, contract: str, *, timeout_seconds: float = 10) -> Quote:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero.")
        self._raise_background_error()
        if self._client is None:
            await self.connect()
        deadline = asyncio.get_running_loop().time() + timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            self._raise_background_error()
            if self._client is None:
                break
            try:
                price = self._client.get_price_info(contract)
            except Exception as error:
                raise TimeoutError(f"Price transport failed for {contract}.") from error
            last_update = self._last_updates.get(contract.strip().upper())
            listener_is_fresh = (
                self._price_listener_id is None
                or last_update is not None
                and time.monotonic() - last_update <= timeout_seconds
            )
            if price is not None and listener_is_fresh:
                return Quote(
                    contract=contract,
                    bid=float(price.bid),
                    ask=float(price.ask),
                    tag=str(price.tag),
                )
            await asyncio.sleep(min(0.1, timeout_seconds))
        raise TimeoutError(f"No fresh quote received for {contract}; restart the process.")


async def read_quote(config: AppConfig, timeout_seconds: float = 10) -> Quote:
    async with PriceStreamSession(config) as session:
        return await session.get_quote(config.trading.contract, timeout_seconds=timeout_seconds)
