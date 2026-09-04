import asyncio
from typing import Any

import pytest

from trader_api_examples.config import AppConfig, EndpointsConfig, Secrets, TradingConfig
from trader_api_examples.price_client import PriceStreamSession


class FakePrice:
    def __init__(self, bid: float, ask: float, tag: str) -> None:
        self.bid = bid
        self.ask = ask
        self.tag = tag


class FakePriceClient:
    def __init__(self) -> None:
        self.init_config: dict[str, str] = {}
        self.login_count = 0
        self.logout_count = 0
        self.prices: dict[str, FakePrice] = {
            "LLG": FakePrice(4000, 4001, "LLG-1"),
            "EURUSD": FakePrice(1.1, 1.2, "EURUSD-1"),
        }

    def init(self, config: dict[str, str]) -> None:
        self.init_config = config

    async def login(self) -> None:
        self.login_count += 1

    async def logout(self) -> None:
        self.logout_count += 1

    def get_price_info(self, contract: str) -> Any:
        return self.prices.get(contract)


class FailingPriceClient(FakePriceClient):
    def get_price_info(self, contract: str) -> Any:
        raise RuntimeError("transport stopped")


class LoginFailingPriceClient(FakePriceClient):
    async def login(self) -> None:
        self.login_count += 1
        raise RuntimeError("login failed")


class PriceEvent:
    def __init__(self, contract_codes: list[str]) -> None:
        self.contract_codes = contract_codes


class ListeningPriceClient(FakePriceClient):
    def __init__(self) -> None:
        super().__init__()
        self.removed_listener = ""

    def add_price_listener(self, callback: Any) -> str:
        callback(PriceEvent(["LLG"]))
        return "listener-1"

    def remove_price_listener(self, callback_id: str) -> None:
        self.removed_listener = callback_id


class ListenerFailingPriceClient(FakePriceClient):
    def add_price_listener(self, callback: Any) -> str:
        raise RuntimeError("listener failed")


class BackgroundDisconnectingPriceClient(FakePriceClient):
    def trigger_disconnect(self) -> None:
        async def fail_send() -> None:
            await asyncio.sleep(0)
            raise RuntimeError("Client is not connected")

        asyncio.create_task(fail_send())


def price_config() -> AppConfig:
    return AppConfig(
        environment="demo",
        endpoints=EndpointsConfig(
            web_proxy_url="https://webproxy.example",
            fxserver_ws_url="wss://fxserver.example/websocket",
            price_agent_ws_url="wss://price.example/websocket",
        ),
        trading=TradingConfig(contract="LLG"),
        secrets=Secrets(api_key="api-key", username="user", trade_key="trade-key"),
    )


@pytest.mark.asyncio
async def test_price_session_reuses_one_login_for_multiple_contracts() -> None:
    client = FakePriceClient()

    async with PriceStreamSession(price_config(), client_factory=lambda: client) as session:
        gold = await session.get_quote("LLG", timeout_seconds=0.1)
        euro = await session.get_quote("EURUSD", timeout_seconds=0.1)

    assert (gold.bid, gold.ask) == (4000, 4001)
    assert (euro.bid, euro.ask) == (1.1, 1.2)
    assert client.login_count == 1
    assert client.logout_count == 0


@pytest.mark.asyncio
async def test_price_session_surfaces_login_failure_without_unsafe_logout() -> None:
    client = LoginFailingPriceClient()

    with pytest.raises(RuntimeError, match="login failed"):
        async with PriceStreamSession(price_config(), client_factory=lambda: client):
            pass

    assert client.logout_count == 0


@pytest.mark.asyncio
async def test_price_session_uses_price_update_listener() -> None:
    client = ListeningPriceClient()

    async with PriceStreamSession(price_config(), client_factory=lambda: client) as session:
        quote = await session.get_quote("LLG", timeout_seconds=0.1)

    assert quote.bid == 4000
    assert client.removed_listener == ""


@pytest.mark.asyncio
async def test_price_session_falls_back_to_polling_when_listener_registration_fails() -> None:
    client = ListenerFailingPriceClient()

    async with PriceStreamSession(price_config(), client_factory=lambda: client) as session:
        quote = await session.get_quote("LLG", timeout_seconds=0.1)

    assert quote.bid == 4000
    assert client.logout_count == 0


@pytest.mark.asyncio
async def test_price_session_fails_closed_on_transport_error() -> None:
    client = FailingPriceClient()

    with pytest.raises(TimeoutError, match="transport failed"):
        async with PriceStreamSession(price_config(), client_factory=lambda: client) as session:
            await session.get_quote("LLG", timeout_seconds=0.1)


@pytest.mark.asyncio
async def test_price_session_converts_unretrieved_disconnect_task_to_transport_failure() -> None:
    client = BackgroundDisconnectingPriceClient()

    async with PriceStreamSession(price_config(), client_factory=lambda: client) as session:
        client.trigger_disconnect()
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        with pytest.raises(TimeoutError, match="background task disconnected"):
            await session.get_quote("LLG", timeout_seconds=0.1)
