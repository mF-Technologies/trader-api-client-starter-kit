from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


class ApiError(RuntimeError):
    """Raised for a safe-to-display Trader API failure."""

    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        error_code: str | None = None,
        *,
        transport_failure: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.transport_failure = transport_failure

    @property
    def is_definitive_rejection(self) -> bool:
        return (
            self.status_code is not None
            and 400 <= self.status_code < 500
            and self.status_code not in {408, 409, 425, 429}
        )

    @property
    def is_transient_response(self) -> bool:
        return self.transport_failure or self.status_code in {408, 425, 429} or (
            self.status_code is not None and self.status_code >= 500
        )

    @property
    def is_duplicate_client_order_id(self) -> bool:
        return self.status_code == 409 and (
            self.error_code or ""
        ).upper() == "DUPLICATE_CLIENT_ORDER_ID"


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

TOKEN_REFRESH_SKEW_SECONDS = 30.0
TOKEN_EXCHANGE_MAX_ATTEMPTS = 3
TOKEN_EXCHANGE_RETRY_DELAY_SECONDS = 1.0


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
        self._access_token_expires_at: float | None = None
        self._access_token_lock = asyncio.Lock()
        self._chart_codes: dict[str, str] | None = None
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

    async def access_token(self, *, force: bool = False) -> str:
        if not force and self._access_token_is_fresh():
            return self._access_token

        async with self._access_token_lock:
            if not force and self._access_token_is_fresh():
                return self._access_token
            attempt = 0
            while True:
                try:
                    response = await self._request(
                        "POST",
                        f"{self._web_api}/tokens/auth",
                        headers={"Authorization": f"Bearer {self._api_key}"},
                        retry_unauthorized=False,
                    )
                    break
                except ApiError as error:
                    attempt += 1
                    if (
                        not error.is_transient_response
                        or attempt >= TOKEN_EXCHANGE_MAX_ATTEMPTS
                    ):
                        raise
                    await asyncio.sleep(
                        TOKEN_EXCHANGE_RETRY_DELAY_SECONDS * (2 ** (attempt - 1))
                    )
            payload = self._object(response, "token exchange")
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise ApiError("Token exchange response did not contain access_token.")
            self._access_token = token
            self._access_token_expires_at = self._token_expiry(payload)
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
        try:
            response = await self._request(
                "GET",
                f"{self.fxserver_url}/positionDetail",
                headers=await self._auth_headers(),
                params={"ref": order_ref},
            )
        except ApiError as error:
            if error.status_code == 404:
                return None
            raise
        self._raise_for_status(response, "position detail")
        return self._object(response, "position detail")

    async def reconcile_market_deal(
        self,
        *,
        contract: str,
        amount: float,
        buy: bool,
        client_order_id: int,
        excluded_refs: tuple[str, ...] = (),
    ) -> str | None:
        """Find an open position created by an ambiguous market-deal response.

        Query contracts do not currently guarantee client-order-id correlation. Prefer an
        explicit client-order-id match when one is returned, then fall back to one unique
        position matching the submitted trade while excluding the pre-submit snapshot.
        """
        order_state: dict[str, list[dict[str, Any]]] = {}
        try:
            order_state = await self.get_orders()
        except ApiError as error:
            if not error.is_transient_response:
                raise

        explicit_candidates = [
            record
            for records in order_state.values()
            for record in records
            if _record_client_order_id(record) == str(client_order_id)
            and _record_matches_trade(record, contract=contract, amount=amount, buy=buy)
        ]
        explicit_ref = _unique_record_ref(explicit_candidates)
        if explicit_ref is not None and await self.get_position_detail(explicit_ref) is not None:
            return explicit_ref

        excluded = set(excluded_refs)
        positions = await self.get_positions()
        matching_positions = [
            position
            for position in positions
            if _record_ref(position) not in excluded
            and _record_matches_trade(position, contract=contract, amount=amount, buy=buy)
        ]
        position_ref = _unique_record_ref(matching_positions)
        if position_ref is None:
            return None
        if await self.get_position_detail(position_ref) is None:
            return None
        return position_ref

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
        if self._chart_codes is None:
            mapping_response = await self._request("GET", f"{self.fxserver_url}/chartCode")
            mapping = self._object(mapping_response, "chart-code mapping")
            self._chart_codes = {
                str(code): instrument
                for code, instrument in mapping.items()
                if isinstance(instrument, str) and instrument
            }
        instrument = self._chart_codes.get(contract)
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
        return str(deal_ref) if deal_ref is not None and str(deal_ref) else None

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
        ref = payload.get("liqRef") or payload.get("liquidateRef") or payload.get("dealRef")
        return str(ref) if ref is not None and str(ref) else None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        retry_unauthorized: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        response = await self._send_request(method, url, **kwargs)
        if response.status_code == 401 and retry_unauthorized:
            stale_token = self._authorization_token(kwargs.get("headers"))
            if stale_token:
                if stale_token == self._access_token:
                    self._access_token = ""
                    self._access_token_expires_at = None
                    token = await self.access_token(force=True)
                else:
                    token = await self.access_token()
                retry_kwargs = dict(kwargs)
                retry_headers = dict(kwargs.get("headers") or {})
                retry_headers["Authorization"] = f"Bearer {token}"
                retry_kwargs["headers"] = retry_headers
                response = await self._send_request(method, url, **retry_kwargs)
        self._raise_for_status(response, httpx.URL(url).path)
        return response

    async def _send_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self._http.request(method, url, **kwargs)
        except httpx.RequestError as error:
            raise ApiError(
                f"Trader API request failed: {method} {httpx.URL(url).path}",
                transport_failure=True,
            ) from error
        except httpx.HTTPError as error:
            raise ApiError(f"Trader API request failed: {method} {httpx.URL(url).path}") from error

    @staticmethod
    def _authorization_token(headers: Any) -> str | None:
        if headers is None:
            return None
        authorization = headers.get("Authorization")
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return None
        token = authorization.removeprefix("Bearer ").strip()
        return token or None

    def _access_token_is_fresh(self) -> bool:
        return bool(self._access_token) and (
            self._access_token_expires_at is None
            or time.monotonic() < self._access_token_expires_at
        )

    @staticmethod
    def _token_expiry(payload: dict[str, Any]) -> float | None:
        expires_in = payload.get("expires_in")
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float, str)):
            return None
        try:
            lifetime_minutes = float(expires_in)
        except (TypeError, ValueError):
            return None
        if lifetime_minutes <= 0:
            return None
        lifetime_seconds = lifetime_minutes * 60
        return time.monotonic() + max(0.0, lifetime_seconds - TOKEN_REFRESH_SKEW_SECONDS)

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
            raise ApiError(
                f"{context} returned HTTP {response.status_code}{suffix}.",
                response.status_code,
                message or None,
            )

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


def _record_ref(record: dict[str, Any]) -> str | None:
    for key in ("ref", "orderNo", "dealRef"):
        value = record.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _record_client_order_id(record: dict[str, Any]) -> str | None:
    for key in ("clientOrderId", "client_order_id"):
        value = record.get(key)
        if value is not None and str(value):
            return str(value)
    return None


def _record_matches_trade(
    record: dict[str, Any], *, contract: str, amount: float, buy: bool
) -> bool:
    record_contract = next(
        (record.get(key) for key in ("contract", "contractCode") if record.get(key) is not None),
        None,
    )
    record_side = next(
        (record.get(key) for key in ("buySell", "buyOrSell", "buySellType") if key in record),
        None,
    )
    record_amount = next(
        (
            record.get(key)
            for key in ("amount", "buyAmount" if buy else "sellAmount")
            if key in record
        ),
        None,
    )
    if not isinstance(record_contract, str) or record_contract.upper() != contract.upper():
        return False
    parsed_side = _as_buy_side(record_side)
    if parsed_side is None or parsed_side is not buy:
        return False
    if record_amount is None:
        return False
    try:
        return math.isclose(float(record_amount), amount, rel_tol=1e-9, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def _as_buy_side(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().upper()
        if normalized in {"BUY", "B", "TRUE", "1"}:
            return True
        if normalized in {"SELL", "S", "FALSE", "0"}:
            return False
    return None


def _unique_record_ref(records: list[dict[str, Any]]) -> str | None:
    refs = {_record_ref(record) for record in records}
    refs.discard(None)
    return next(iter(refs)) if len(refs) == 1 else None
