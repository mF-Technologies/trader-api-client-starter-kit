from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


class ApiError(RuntimeError):
    """Raised for a safe-to-display Trader API failure."""


@dataclass(frozen=True)
class Bar:
    time_ms: int
    open: float
    high: float
    low: float
    close: float


PERIOD_DURATION = {
    1: timedelta(minutes=1),
    2: timedelta(hours=1),
    3: timedelta(days=1),
    4: timedelta(weeks=1),
    5: timedelta(days=31),
    6: timedelta(minutes=5),
    7: timedelta(minutes=15),
    8: timedelta(minutes=30),
    9: timedelta(hours=2),
    10: timedelta(hours=4),
    11: timedelta(minutes=2),
    12: timedelta(minutes=3),
    13: timedelta(minutes=4),
    14: timedelta(minutes=6),
    15: timedelta(minutes=10),
    16: timedelta(minutes=12),
    17: timedelta(minutes=20),
    18: timedelta(hours=3),
    19: timedelta(hours=8),
    20: timedelta(hours=12),
}


class TraderApiClient:
    def __init__(
        self,
        *,
        web_proxy_url: str,
        fxserver_url: str,
        chart_server_url: str,
        api_key: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.web_proxy_url = web_proxy_url.rstrip("/")
        self.fxserver_url = fxserver_url.rstrip("/")
        self.chart_server_url = chart_server_url.rstrip("/")
        self._api_key = api_key
        self._access_token = ""
        self._last_liquidation_payload: dict[str, Any] | None = None
        self._http = http_client or httpx.AsyncClient(timeout=20)
        self._owns_http = http_client is None

    async def __aenter__(self) -> TraderApiClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_http:
            await self._http.aclose()

    @property
    def _web_api(self) -> str:
        return (
            self.web_proxy_url
            if self.web_proxy_url.endswith("/api")
            else f"{self.web_proxy_url}/api"
        )

    async def access_token(self) -> str:
        if self._access_token:
            return self._access_token
        response = await self._request(
            "POST",
            f"{self._web_api}/tokens/auth",
            headers={"Authorization": f"Bearer {self._api_key}"},
        )
        payload = self._object(response, "token exchange")
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise ApiError("Token exchange response did not contain access_token.")
        self._access_token = token
        return token

    async def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {await self.access_token()}"}

    async def get_account_balance(self) -> dict[str, Any]:
        response = await self._request(
            "GET", f"{self.fxserver_url}/accountBalance", headers=await self._auth_headers()
        )
        return self._object(response, "account balance")

    async def get_contract_settings(self) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"{self._web_api}/contractSetting", headers=await self._auth_headers()
        )
        payload = response.json()
        if isinstance(payload, list):
            return [dict(item) for item in payload if isinstance(item, dict)]
        if isinstance(payload, dict):
            for key in ("contractSettings", "contracts", "data"):
                items = payload.get(key)
                if isinstance(items, list):
                    return [dict(item) for item in items if isinstance(item, dict)]
        raise ApiError("Contract settings response had an unexpected shape.")

    async def get_positions(self) -> list[dict[str, Any]]:
        response = await self._request(
            "GET", f"{self._web_api}/getPositionToday", headers=await self._auth_headers()
        )
        return self._list_envelope(response, "positions", "position state")

    async def get_orders(self) -> dict[str, list[dict[str, Any]]]:
        response = await self._request(
            "GET", f"{self._web_api}/getOrderToday", headers=await self._auth_headers()
        )
        payload = self._object(response, "order state")
        result: dict[str, list[dict[str, Any]]] = {}
        for key in ("workingOrders", "executedOrders", "cancelledOrders"):
            items = payload.get(key, [])
            if not isinstance(items, list):
                raise ApiError("Order state response had an unexpected shape.")
            result[key] = [dict(item) for item in items if isinstance(item, dict)]
        return result

    async def get_position_detail(self, order_ref: str) -> dict[str, Any] | None:
        response = await self._http.get(
            f"{self.fxserver_url}/positionDetail",
            headers=await self._auth_headers(),
            params={"ref": order_ref},
        )
        if response.status_code == 404:
            return None
        self._raise_for_status(response, "position detail")
        return self._object(response, "position detail")

    @property
    def last_liquidation_payload(self) -> dict[str, Any] | None:
        """Return the raw response from the most recent liquidation request."""
        return dict(self._last_liquidation_payload) if self._last_liquidation_payload else None

    async def get_completed_bars(
        self,
        *,
        contract: str,
        period_type: int,
        count: int,
        now: datetime | None = None,
    ) -> list[Bar]:
        if period_type not in PERIOD_DURATION:
            raise ApiError(f"Unsupported period type: {period_type}")
        mapping_response = await self._request("GET", f"{self.fxserver_url}/chartCode")
        mapping = self._object(mapping_response, "chart-code mapping")
        instrument = mapping.get(contract)
        if not isinstance(instrument, str) or not instrument:
            raise ApiError(f"No chart-code mapping exists for contract {contract}.")
        response = await self._request(
            "GET",
            f"{self.chart_server_url}/api/instrument/{instrument}/getChartBarsByEndTimeAndBarNumber/{period_type}/{count}",
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise ApiError("Historical bars response had an unexpected shape.")
        current_time = now or datetime.now(UTC)
        duration = PERIOD_DURATION[period_type]
        bars: list[Bar] = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            try:
                bar = Bar(
                    time_ms=int(item["time"]),
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ApiError("Historical bars contained a malformed bar.") from error
            started = datetime.fromtimestamp(bar.time_ms / 1000, tz=UTC)
            if started + duration <= current_time:
                bars.append(bar)
        return sorted(bars, key=lambda bar: bar.time_ms)

    async def add_market_deal(
        self,
        *,
        contract: str,
        amount: float,
        buy: bool,
        client_order_id: int,
    ) -> str | None:
        response = await self._request(
            "POST",
            f"{self.fxserver_url}/addDeal",
            headers=await self._auth_headers(),
            json={
                "clientOrderId": client_order_id,
                "priceMode": 1,
                "contractCode": contract,
                "amount": amount,
                "buyOrSell": buy,
            },
        )
        payload = self._object(response, "addDeal")
        deal_ref = payload.get("dealRef")
        # Some demo responses confirm HTTP success but omit a deal reference.
        # The execution manager can reconcile the new position from position state.
        return str(deal_ref) if deal_ref is not None else None

    async def liquidate_market_deal(
        self, *, order_ref: str, amount: float, client_order_id: int
    ) -> str | None:
        response = await self._request(
            "POST",
            f"{self.fxserver_url}/liquidate",
            headers=await self._auth_headers(),
            json={
                "clientOrderId": client_order_id,
                "priceMode": 1,
                "orderNo": order_ref,
                "amount": amount,
            },
        )
        payload = self._object(response, "liquidate")
        self._last_liquidation_payload = payload
        ref = payload.get("liqRef") or payload.get("liquidateRef")
        # Some demo responses confirm HTTP success but omit a liquidation reference.
        # Position disappearance is the authoritative confirmation in that case.
        return str(ref) if ref is not None else None

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as error:
            raise ApiError(f"Trader API request failed: {method} {httpx.URL(url).path}") from error
        self._raise_for_status(response, httpx.URL(url).path)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response, context: str) -> None:
        if response.is_error:
            message = ""
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    message = str(payload.get("msg") or payload.get("message") or "")
            except ValueError:
                pass
            suffix = f" ({message})" if message else ""
            raise ApiError(f"{context} returned HTTP {response.status_code}{suffix}.")

    @staticmethod
    def _object(response: httpx.Response, context: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as error:
            raise ApiError(f"{context} response was not JSON.") from error
        if not isinstance(payload, dict):
            raise ApiError(f"{context} response had an unexpected shape.")
        return dict(payload)

    @classmethod
    def _list_envelope(
        cls, response: httpx.Response, key: str, context: str
    ) -> list[dict[str, Any]]:
        payload = cls._object(response, context)
        items = payload.get(key)
        if not isinstance(items, list):
            raise ApiError(f"{context} response had an unexpected shape.")
        return [dict(item) for item in items if isinstance(item, dict)]
