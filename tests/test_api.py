from datetime import UTC, datetime

import httpx
import pytest

from trader_api_examples.api import TraderApiClient


@pytest.mark.asyncio
async def test_client_exchanges_key_and_reads_account_state() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/tokens/auth":
            assert request.headers["Authorization"] == "Bearer api-key"
            return httpx.Response(200, json={"access_token": "fx-token", "expires_in": 60})
        if request.url.path == "/accountBalance":
            assert request.headers["Authorization"] == "Bearer fx-token"
            return httpx.Response(200, json={"balance": 1234.5, "currency": "USD"})
        if request.url.path == "/api/getPositionToday":
            return httpx.Response(200, json={"positions": [{"ref": "42"}]})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        balance = await client.get_account_balance()
        positions = await client.get_positions()

    assert balance == {"balance": 1234.5, "currency": "USD"}
    assert positions == [{"ref": "42"}]
    assert [request.url.path for request in requests].count("/api/tokens/auth") == 1


@pytest.mark.asyncio
async def test_client_fetches_completed_chart_bars_only() -> None:
    now = datetime(2026, 8, 26, 12, 0, 30, tzinfo=UTC)
    completed_start = int(datetime(2026, 8, 26, 11, 59, tzinfo=UTC).timestamp() * 1000)
    open_start = int(datetime(2026, 8, 26, 12, 0, tzinfo=UTC).timestamp() * 1000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/chartCode":
            return httpx.Response(200, json={"EURUSD": "EURUSDUSD"})
        if "getChartBarsByEndTimeAndBarNumber" in request.url.path:
            return httpx.Response(
                200,
                json=[
                    {"time": completed_start, "open": 1, "high": 2, "low": 0, "close": 1.5},
                    {"time": open_start, "open": 1.5, "high": 2, "low": 1, "close": 1.7},
                ],
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        bars = await client.get_completed_bars(contract="EURUSD", period_type=1, count=100, now=now)

    assert [bar.time_ms for bar in bars] == [completed_start]


@pytest.mark.asyncio
async def test_client_accepts_successful_liquidation_without_reference() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tokens/auth":
            return httpx.Response(200, json={"access_token": "fx-token"})
        if request.url.path == "/liquidate":
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        liquidation_ref = await client.liquidate_market_deal(
            order_ref="deal-42", amount=10, client_order_id=123
        )

    assert liquidation_ref is None
    assert client.last_liquidation_payload == {"success": True}


@pytest.mark.asyncio
async def test_client_accepts_successful_deal_without_reference() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tokens/auth":
            return httpx.Response(200, json={"access_token": "fx-token"})
        if request.url.path == "/addDeal":
            return httpx.Response(200, json={"success": True})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = TraderApiClient(
            web_proxy_url="https://web.example",
            fxserver_url="https://fx.example",
            chart_server_url="https://chart.example",
            api_key="api-key",  # pragma: allowlist secret
            http_client=http,
        )
        deal_ref = await client.add_market_deal(
            contract="EURUSD", amount=10, buy=True, client_order_id=123
        )

    assert deal_ref is None
