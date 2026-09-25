from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import secrets
import sys
import time
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from .algo import (
    AlgoResult,
    Outcome,
    SignalEvent,
    evaluate_latest_strategy,
    evaluate_replay,
    latest_signal,
    required_completed_bars,
)
from .api import PERIOD_DURATION, ApiError, Bar, TraderApiClient
from .config import AlgoInstanceConfig, AppConfig
from .contracts import calculate_amount, find_contract_setting, validate_amount
from .execution import ExecutionManager, is_retryable_cleanup_error
from .output import CommandResult
from .position_sync import PositionUpdateNotification
from .price_client import PriceStreamSession, Quote, QuoteUnavailableError, read_quote
from .runtime import HeartbeatWriter, InstanceEventLog, instance_journal_path
from .safety import (
    Journal,
    JournalState,
    LiveExecutionBlocked,
    assert_live_execution_enabled,
)
from .strategy import PositionSide, Signal

POSITION_STREAM_RETRY_DELAY_SECONDS = 1.0
POSITION_STREAM_MAX_RETRY_DELAY_SECONDS = 30.0
POSITION_RECONCILE_MIN_INTERVAL_SECONDS = 0.05


class PositionReconcileThrottle:
    """Limit shared position reads to the account's 20 requests per second budget."""

    def __init__(self, min_interval: float = POSITION_RECONCILE_MIN_INTERVAL_SECONDS) -> None:
        self._min_interval = min_interval
        self._next_allowed_at = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            delay = max(0.0, self._next_allowed_at - now)
            self._next_allowed_at = max(now, self._next_allowed_at) + self._min_interval
        if delay:
            await asyncio.sleep(delay)


def account_fingerprint(api_key: str) -> str:
    return f"key-{hashlib.sha256(api_key.encode()).hexdigest()[:12]}"


def make_client(config: AppConfig, *, require_chart: bool = False) -> TraderApiClient:
    required = {
        "endpoints.web_proxy_url": config.endpoints.web_proxy_url,
        "endpoints.fxserver_rest_url": config.endpoints.fxserver_rest_url,
    }
    if require_chart:
        required["endpoints.chart_server_url"] = config.endpoints.chart_server_url
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("Required endpoint settings are missing: " + ", ".join(missing))
    return TraderApiClient(
        web_proxy_url=config.endpoints.web_proxy_url,
        fxserver_url=config.endpoints.fxserver_rest_url,
        chart_server_url=config.endpoints.chart_server_url,
        api_key=config.secrets.api_key,
    )


def _journal_path(config: AppConfig) -> Path:
    name = "".join(
        character.lower() if character.isalnum() else "-" for character in config.trading.contract
    )
    return Path("runtime") / f"execution-{name}.json"


def _client_order_id() -> int:
    return secrets.randbelow(2_147_483_646) + 1


def _write_execution_summary(config: AppConfig, fingerprint: str, side: str) -> None:
    print(
        "Execution summary: "
        f"account={fingerprint} environment={config.environment} "
        f"contract={config.trading.contract} side={side} amount={config.trading.amount}",
        file=sys.stderr,
    )


def _event_data(result: AlgoResult) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event in result.events:
        item: dict[str, Any] = {
            "time_ms": event.time_ms,
            "signal": event.signal.value,
            "strategy": event.strategy,
            "indicator_value": event.indicator_value,
            "close": event.close,
        }
        if event.strategy == "rsi":
            item["rsi"] = event.indicator_value
        events.append(item)
    return events


@dataclass
class _InstanceRuntime:
    config: AlgoInstanceConfig
    deadline: float | None
    manager: ExecutionManager | None = None
    journal: Journal | None = None
    owned_side: PositionSide | None = None
    last_bar_time: int = -1
    last_quote: Quote | None = None
    history_loaded: bool = False
    market_data_paused_at: float | None = None
    next_market_data_retry_at: float = 0.0
    next_position_recovery_retry_at: float = 0.0
    next_position_reconcile_at: float = 0.0
    next_cleanup_retry_at: float = 0.0
    next_price_log_at: float = 0.0
    failure_reason: str | None = None
    events: list[SignalEvent] = field(default_factory=list)
    status: str = "starting"
    completed: bool = False
    event_log: InstanceEventLog | None = None


async def account_inspector(config: AppConfig) -> CommandResult:
    async with make_client(config) as client:
        balance, positions, orders = await asyncio.gather(
            client.get_account_balance(), client.get_positions(), client.get_orders()
        )
    return CommandResult(
        "account-inspector",
        Outcome.SUCCESS,
        "Account state retrieved.",
        {
            "account_fingerprint": account_fingerprint(config.secrets.api_key),
            "environment": config.environment,
            "balance": {
                key: value
                for key, value in balance.items()
                if key.lower() in {"available", "balance", "currency", "equity", "margin"}
            },
            "open_positions": len(positions),
            "working_orders": len(orders["workingOrders"]),
        },
    )


async def contract_calculator(config: AppConfig, lots: float) -> CommandResult:
    async with make_client(config) as client:
        setting = find_contract_setting(
            await client.get_contract_settings(), config.trading.contract
        )
    calculation = calculate_amount(lots=lots, contract_setting=setting)
    return CommandResult(
        "contract-calculator",
        Outcome.SUCCESS,
        "Contract amount calculated.",
        {
            "contract": config.trading.contract,
            "lots": calculation.lots,
            "amount": calculation.amount,
            "minimum_amount": calculation.minimum_amount,
            "increment_amount": calculation.increment_amount,
        },
    )


async def market_data_monitor(config: AppConfig, with_quote: bool) -> CommandResult:
    async with make_client(config, require_chart=True) as client:
        bars = await client.get_completed_bars(
            contract=config.trading.contract,
            period_type=config.strategy.period_type,
            count=config.trading.bar_count,
        )
    data: dict[str, Any] = {
        "contract": config.trading.contract,
        "completed_bar_count": len(bars),
        "latest_completed_bar": vars(bars[-1]) if bars else None,
    }
    if with_quote:
        quote = await read_quote(config)
        data["quote"] = {
            "contract": quote.contract,
            "bid": quote.bid,
            "ask": quote.ask,
            "tag_present": bool(quote.tag),
        }
    outcome = Outcome.SUCCESS if bars else Outcome.INCONCLUSIVE
    message = "Market data retrieved." if bars else "No completed historical bars were returned."
    return CommandResult("market-data-monitor", outcome, message, data)


def load_replay_fixture(path: Path | None) -> list[Bar]:
    fixture_path = path or Path(
        str(files("trader_api_examples") / "fixtures" / "rsi_reversal.json")
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Replay fixture must be a JSON array.")
    return [
        Bar(
            time_ms=int(item["time"]),
            open=float(item["open"]),
            high=float(item["high"]),
            low=float(item["low"]),
            close=float(item["close"]),
        )
        for item in payload
        if isinstance(item, dict)
    ]


def replay_algo(config: AppConfig, fixture: Path | None) -> CommandResult:
    result = evaluate_replay(
        load_replay_fixture(fixture),
        rsi_period=config.strategy.rsi_period,
        oversold=config.strategy.oversold,
        overbought=config.strategy.overbought,
        exit_level=config.strategy.exit_level,
    )
    return CommandResult(
        "rsi-algo-demo",
        result.outcome,
        result.message,
        {"mode": "replay", "contract": config.trading.contract, "events": _event_data(result)},
    )


async def order_lifecycle(config: AppConfig, execute: bool) -> CommandResult:
    assert_live_execution_enabled(enabled=config.live_trading_enabled, execute=execute)
    fingerprint = account_fingerprint(config.secrets.api_key)
    journal_path = _journal_path(config)
    journal: Journal | None = None
    async with make_client(config) as client:
        setting = find_contract_setting(
            await client.get_contract_settings(), config.trading.contract
        )
        validate_amount(amount=config.trading.amount, contract_setting=setting)
        manager = ExecutionManager(
            client=client,
            journal_path=journal_path,
            live_trading_enabled=config.live_trading_enabled,
        )
        _write_execution_summary(config, fingerprint, "BUY")
        try:
            journal = await manager.open_position(
                execute=execute,
                account_fingerprint=fingerprint,
                contract=config.trading.contract,
                amount=config.trading.amount,
                buy=True,
                client_order_id=_client_order_id(),
            )
            cleanup_ref = await manager.cleanup(
                journal=journal,
                execute=execute,
                client_order_id=_client_order_id(),
            )
            journal = None
        finally:
            pending: Journal | None = Journal.load(journal_path) if journal_path.exists() else None
            if pending and pending.order_ref:
                await manager.cleanup(
                    journal=pending,
                    execute=execute,
                    client_order_id=_client_order_id(),
                )
    return CommandResult(
        "order-lifecycle-checker",
        Outcome.SUCCESS,
        "Market order was reconciled and cleaned up.",
        {
            "account_fingerprint": fingerprint,
            "environment": config.environment,
            "contract": config.trading.contract,
            "amount": config.trading.amount,
            "cleanup_ref": cleanup_ref,
        },
    )


def _latest_is_stale(bars: list[Bar], config: AppConfig) -> bool:
    age_seconds = _latest_bar_age_seconds(bars)
    return age_seconds is None or age_seconds > _stale_threshold_seconds(
        config.strategy.period_type,
        config.trading.bar_stale_grace_seconds,
    )


def _latest_bar_age_seconds(bars: list[Bar]) -> float | None:
    if not bars:
        return None
    return time.time() - bars[-1].time_ms / 1000


def _stale_threshold_seconds(period_type: int, grace_seconds: float) -> float:
    return PERIOD_DURATION[period_type].total_seconds() * 2 + grace_seconds


def _contiguous_bar_suffix(
    bars: list[Bar], period_type: int
) -> tuple[list[Bar], tuple[int, int] | None]:
    """Return the latest contiguous bars and the first newer-than-expected gap."""
    if not bars:
        return [], None
    expected_ms = int(PERIOD_DURATION[period_type].total_seconds() * 1000)
    suffix = [bars[-1]]
    gap: tuple[int, int] | None = None
    for previous, current in zip(reversed(bars[:-1]), reversed(bars[1:]), strict=True):
        if current.time_ms - previous.time_ms > expected_ms:
            gap = (previous.time_ms, current.time_ms)
            break
        suffix.append(previous)
    suffix.reverse()
    return suffix, gap


def _bar_gap_message(instance_name: str, gap: tuple[int, int], contiguous_count: int) -> str:
    previous_time_ms, current_time_ms = gap
    return (
        f"Completed market data has a gap for {instance_name}: "
        f"previous_time_ms={previous_time_ms} current_time_ms={current_time_ms}; "
        f"only {contiguous_count} contiguous bars are available."
    )


async def live_algo(config: AppConfig, mode: str, execute: bool) -> CommandResult:
    if mode == "live-execute":
        assert_live_execution_enabled(enabled=config.live_trading_enabled, execute=execute)
    loop = asyncio.get_running_loop()
    runtime_seconds = config.trading.max_runtime_seconds
    deadline = None if runtime_seconds is None else loop.time() + runtime_seconds
    fingerprint = account_fingerprint(config.secrets.api_key)
    manager: ExecutionManager | None = None
    journal: Journal | None = None
    last_bar_time = -1
    owned_side: PositionSide | None = None
    unrelated_position_count = 0
    async with make_client(config, require_chart=True) as client:
        setting = find_contract_setting(
            await client.get_contract_settings(), config.trading.contract
        )
        validate_amount(amount=config.trading.amount, contract_setting=setting)
        if mode == "live-execute":
            manager = ExecutionManager(
                client=client,
                journal_path=_journal_path(config),
                live_trading_enabled=config.live_trading_enabled,
            )
            unrelated_position_count = len(await client.get_positions())
            if unrelated_position_count:
                print(
                    f"Warning: account has {unrelated_position_count} unrelated open "
                    "position(s); this run will not manage them.",
                    file=sys.stderr,
                )
        try:
            while deadline is None or loop.time() < deadline:
                bars = await client.get_completed_bars(
                    contract=config.trading.contract,
                    period_type=config.strategy.period_type,
                    count=config.trading.bar_count,
                )
                if _latest_is_stale(bars, config):
                    return CommandResult(
                        "rsi-algo-demo",
                        Outcome.INCONCLUSIVE,
                        "Completed market data is stale or missing.",
                        {"mode": mode},
                    )
                bars, gap = _contiguous_bar_suffix(bars, config.strategy.period_type)
                if gap is not None and len(bars) < required_completed_bars(config.strategy):
                    return CommandResult(
                        "rsi-algo-demo",
                        Outcome.INCONCLUSIVE,
                        _bar_gap_message("default", gap, len(bars)),
                        {"mode": mode},
                    )
                if len(bars) < required_completed_bars(config.strategy):
                    return CommandResult(
                        "rsi-algo-demo",
                        Outcome.INCONCLUSIVE,
                        "Not enough completed bars for the configured strategy.",
                        {"mode": mode},
                    )
                if bars[-1].time_ms == last_bar_time:
                    await asyncio.sleep(config.trading.poll_seconds)
                    continue
                last_bar_time = bars[-1].time_ms
                event = latest_signal(
                    bars,
                    rsi_period=config.strategy.rsi_period,
                    position_side=owned_side,
                    oversold=config.strategy.oversold,
                    overbought=config.strategy.overbought,
                    exit_level=config.strategy.exit_level,
                )
                if event is None:
                    await asyncio.sleep(config.trading.poll_seconds)
                    continue
                if mode == "live-observe":
                    return CommandResult(
                        "rsi-algo-demo",
                        Outcome.SUCCESS,
                        "Live RSI signal observed; no order was submitted.",
                        {
                            "mode": mode,
                            "proposed_event": _event_data(AlgoResult(Outcome.SUCCESS, [event], ""))[
                                0
                            ],
                        },
                    )
                if manager is None:
                    raise RuntimeError("Execution manager was not initialized.")
                if event.signal in {Signal.OPEN_BUY, Signal.OPEN_SELL} and owned_side is None:
                    _write_execution_summary(
                        config,
                        fingerprint,
                        "BUY" if event.signal is Signal.OPEN_BUY else "SELL",
                    )
                    journal = await manager.open_position(
                        execute=execute,
                        account_fingerprint=fingerprint,
                        contract=config.trading.contract,
                        amount=config.trading.amount,
                        buy=event.signal is Signal.OPEN_BUY,
                        client_order_id=_client_order_id(),
                    )
                    owned_side = (
                        PositionSide.LONG if event.signal is Signal.OPEN_BUY else PositionSide.SHORT
                    )
                elif event.signal in {Signal.CLOSE_BUY, Signal.CLOSE_SELL} and journal:
                    await manager.cleanup(
                        journal=journal,
                        execute=execute,
                        client_order_id=_client_order_id(),
                    )
                    journal = None
                    return CommandResult(
                        "rsi-algo-demo",
                        Outcome.SUCCESS,
                        "Live RSI round trip completed and reconciled.",
                        {
                            "mode": mode,
                            "account_fingerprint": fingerprint,
                            "unrelated_position_count": unrelated_position_count,
                        },
                    )
                await asyncio.sleep(config.trading.poll_seconds)
            if journal and manager:
                await manager.cleanup(
                    journal=journal,
                    execute=execute,
                    client_order_id=_client_order_id(),
                )
                journal = None
                return CommandResult(
                    "rsi-algo-demo",
                    Outcome.SUCCESS,
                    "Maximum runtime reached; owned position was cleaned up.",
                    {"mode": mode},
                )
        finally:
            journal_path = _journal_path(config)
            pending = Journal.load(journal_path) if journal_path.exists() else None
            if pending and pending.order_ref and manager:
                await manager.cleanup(
                    journal=pending,
                    execute=execute,
                    client_order_id=_client_order_id(),
                )
    return CommandResult(
        "rsi-algo-demo",
        Outcome.NO_SIGNAL,
        "No RSI crossing occurred before the runtime limit.",
        {"mode": mode},
    )


def _runtime_data(state: _InstanceRuntime) -> dict[str, Any]:
    quote = state.last_quote
    return {
        "contract": state.config.trading.contract,
        "strategy": state.config.strategy.name,
        "status": state.status,
        "failure_reason": state.failure_reason,
        "quote": (
            {"bid": quote.bid, "ask": quote.ask, "tag_present": bool(quote.tag)} if quote else None
        ),
        "events": _event_data(AlgoResult(Outcome.SUCCESS, state.events, "")),
    }


def _latest_is_stale_for_period(
    bars: list[Bar], period_type: int, grace_seconds: float = 60.0
) -> bool:
    age_seconds = _latest_bar_age_seconds(bars)
    return age_seconds is None or age_seconds > _stale_threshold_seconds(period_type, grace_seconds)


def _write_instance_execution_summary(
    config: AppConfig,
    state: _InstanceRuntime,
    fingerprint: str,
    event: SignalEvent,
) -> None:
    instance = state.config
    side = "BUY" if event.signal is Signal.OPEN_BUY else "SELL"
    print(
        "Execution summary: "
        f"account={fingerprint} environment={config.environment} instance={instance.name} "
        f"contract={instance.trading.contract} side={side} amount={instance.trading.amount}",
        file=sys.stderr,
    )


def _write_console_status(scope: str, message: str) -> None:
    print(f"[{scope}] {message}", file=sys.stderr, flush=True)


def _position_amount(position: dict[str, Any]) -> float | None:
    value = position.get("amount")
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def _indicator_summary(indicators: dict[str, float]) -> str:
    if not indicators:
        return "none"
    return ",".join(f"{name}={value}" for name, value in sorted(indicators.items()))


async def _cleanup_runtime_state(state: _InstanceRuntime, *, execute: bool) -> None:
    if state.journal is None or state.manager is None:
        return
    if state.journal.path.exists():
        persisted = Journal.load(state.journal.path)
        if persisted.run_id == state.journal.run_id:
            state.journal = persisted
    client_order_id = state.journal.cleanup_client_order_id or _client_order_id()
    if state.event_log is not None:
        state.event_log.write(
            "LIQUIDATE_SUBMITTED",
            deal_ref=state.journal.order_ref,
            client_order_id=client_order_id,
        )
    _write_console_status(
        f"REST:{state.config.name}",
        f"Liquidating deal_ref={state.journal.order_ref} client_order_id={client_order_id}",
    )
    cleanup_ref = await state.manager.cleanup(
        journal=state.journal,
        execute=execute,
        client_order_id=client_order_id,
    )
    if state.event_log is not None:
        state.event_log.write(
            "POSITION_CLOSED",
            deal_ref=state.journal.order_ref,
            cleanup_ref=cleanup_ref,
        )
    _write_console_status(f"REST:{state.config.name}", f"Position closed cleanup_ref={cleanup_ref}")
    state.journal = None
    state.owned_side = None


async def _reconcile_external_position(
    state: _InstanceRuntime,
    *,
    client: TraderApiClient,
    throttle: PositionReconcileThrottle,
    now: float,
    force: bool = False,
) -> bool:
    """Confirm whether an owned position still exists outside the runner.

    Event stream notifications only wake this check. A transient read failure must
    preserve ownership because clearing the journal would make a real position unsafe.
    """
    journal = state.journal
    if journal is None or not journal.order_ref:
        return True
    if not force and now < state.next_position_reconcile_at:
        return True
    state.next_position_reconcile_at = now + state.config.trading.position_reconcile_seconds
    try:
        await throttle.wait()
        position = await client.get_position_detail(journal.order_ref)
    except ApiError as error:
        if not error.is_transient_response:
            raise
        if state.event_log is not None:
            state.event_log.write(
                "POSITION_RECONCILIATION_DEFERRED",
                deal_ref=journal.order_ref,
                status_code=error.status_code,
                error_code=error.error_code,
            )
        return False
    if position is not None:
        current_amount = _position_amount(position)
        if current_amount is not None and current_amount != journal.amount:
            previous_amount = journal.amount
            journal = journal.with_state(journal.state, amount=current_amount)
            state.journal = journal
            if state.event_log is not None:
                state.event_log.write(
                    "POSITION_EXTERNALLY_MODIFIED",
                    deal_ref=journal.order_ref,
                    previous_amount=previous_amount,
                    current_amount=current_amount,
                )
            _write_console_status(
                f"REST:{state.config.name}",
                f"External position update confirmed deal_ref={journal.order_ref} "
                f"amount={current_amount}; local ownership updated.",
            )
        return True

    deal_ref = journal.order_ref
    journal.clear()
    state.journal = None
    state.owned_side = None
    state.status = "external-close-reconciled"
    state.failure_reason = None
    if state.event_log is not None:
        state.event_log.write("POSITION_EXTERNALLY_CLOSED", deal_ref=deal_ref)
    _write_console_status(
        f"REST:{state.config.name}",
        f"External position close confirmed deal_ref={deal_ref}; strategy state reset to FLAT.",
    )
    return True


async def _run_position_event_stream(
    client: TraderApiClient,
    queue: asyncio.Queue[PositionUpdateNotification],
    stop_event: asyncio.Event,
) -> None:
    watch = getattr(client, "watch_position_updates", None)
    if watch is None:
        return
    retry_delay = POSITION_STREAM_RETRY_DELAY_SECONDS
    try:
        while not stop_event.is_set():
            try:
                await watch(queue, stop_event)
                if stop_event.is_set():
                    return
                _write_console_status(
                    "Position",
                    "Position event stream ended; retrying while REST reconciliation "
                    "remains active.",
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _write_console_status(
                    "Position",
                    f"Position event stream unavailable; retrying in {retry_delay:g}s "
                    f"while REST reconciliation remains active: {error}",
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop_event.wait(), timeout=retry_delay)
            retry_delay = min(
                POSITION_STREAM_MAX_RETRY_DELAY_SECONDS,
                retry_delay * 2,
            )
    except asyncio.CancelledError:
        raise


async def _restore_runtime_state(
    state: _InstanceRuntime,
    *,
    account_fingerprint: str,
    execute: bool,
    now: float,
) -> bool:
    manager = state.manager
    if manager is None or not manager.journal_path.exists():
        return True
    journal = Journal.load(manager.journal_path)
    instance = state.config
    if journal.account_fingerprint != account_fingerprint:
        raise LiveExecutionBlocked(
            f"Execution journal for {instance.name} belongs to a different account."
        )
    if journal.contract.upper() != instance.trading.contract.upper():
        raise LiveExecutionBlocked(
            f"Execution journal for {instance.name} belongs to a different contract."
        )
    if journal.state in {JournalState.PENDING_SUBMISSION, JournalState.OWNERSHIP_UNCONFIRMED}:
        previous_state = journal.state
        reconciled = await manager.reconcile_submission(journal=journal)
        if reconciled is None:
            _schedule_position_recovery_retry(
                state,
                error=ApiError(
                    f"Execution ownership for {instance.name} is still unresolved.",
                    503,
                ),
                now=now,
            )
            return False
        journal = reconciled
        if state.event_log is not None:
            state.event_log.write(
                "POSITION_RECONCILED",
                deal_ref=journal.order_ref,
                previous_state=previous_state.value,
            )
        _write_console_status(
            f"REST:{instance.name}",
            f"Reconciled submitted position deal_ref={journal.order_ref}; resuming.",
        )
    if journal.side not in {"BUY", "SELL"} or not journal.order_ref:
        raise LiveExecutionBlocked(f"Execution journal for {instance.name} is invalid.")

    position = await manager.client.get_position_detail(journal.order_ref)
    if position is None:
        journal.clear()
        state.status = "journal-reconciled"
        state.failure_reason = None
        if state.event_log is not None:
            state.event_log.write(
                "JOURNAL_RECONCILED",
                previous_state=journal.state.value,
                deal_ref=journal.order_ref,
            )
        _write_console_status(
            f"REST:{instance.name}",
            f"Previous position deal_ref={journal.order_ref} is already closed; journal cleared.",
        )
        return True

    state.journal = journal
    state.owned_side = PositionSide.LONG if journal.side == "BUY" else PositionSide.SHORT
    state.next_position_reconcile_at = now + state.config.trading.position_reconcile_seconds
    state.status = "position-restored"
    state.failure_reason = None
    if state.event_log is not None:
        state.event_log.write(
            "POSITION_RESTORED",
            deal_ref=journal.order_ref,
            side=journal.side,
            amount=journal.amount,
            previous_state=journal.state.value,
        )
    _write_console_status(
        f"REST:{instance.name}",
        f"Restored owned position deal_ref={journal.order_ref} side={journal.side} "
        f"amount={journal.amount}",
    )
    if journal.state is JournalState.CLEANUP_PENDING:
        _write_console_status(
            f"REST:{instance.name}",
            "Previous cleanup was interrupted; retrying before strategy resumes.",
        )
        await _cleanup_runtime_state(state, execute=execute)
        state.status = "cleanup-reconciled"
    return True


def _api_error_label(error: ApiError) -> str:
    if error.error_code:
        return error.error_code
    if error.status_code is not None:
        return f"HTTP {error.status_code}"
    return "transient API error"


def _schedule_cleanup_retry(state: _InstanceRuntime, *, error: ApiError, now: float) -> None:
    state.status = "cleanup-pending"
    state.next_cleanup_retry_at = now + state.config.trading.market_data_retry_seconds
    if state.event_log is not None:
        state.event_log.write(
            "CLEANUP_RETRY_SCHEDULED",
            deal_ref=state.journal.order_ref if state.journal else None,
            error_code=error.error_code,
            retry_seconds=state.config.trading.market_data_retry_seconds,
        )
    _write_console_status(
        f"REST:{state.config.name}",
        f"Cleanup temporarily unavailable ({_api_error_label(error)}); position remains tracked. "
        f"Retrying in {state.config.trading.market_data_retry_seconds:g}s. "
        "Other instances continue.",
    )


def _schedule_position_recovery_retry(
    state: _InstanceRuntime,
    *,
    error: ApiError,
    now: float,
) -> None:
    first_failure = state.status != "position-recovery-pending"
    state.status = "position-recovery-pending"
    state.failure_reason = "position-recovery-pending"
    state.next_position_recovery_retry_at = now + state.config.trading.market_data_retry_seconds
    if state.event_log is not None:
        state.event_log.write(
            "POSITION_RECOVERY_PENDING" if first_failure else "POSITION_RECOVERY_RETRY_FAILED",
            error_code=error.error_code,
            status_code=error.status_code,
            retry_seconds=state.config.trading.market_data_retry_seconds,
        )
    _write_console_status(
        f"REST:{state.config.name}",
        f"Position recovery temporarily unavailable ({_api_error_label(error)}); "
        "journal retained and no trading will resume until position is confirmed. "
        f"Retrying in {state.config.trading.market_data_retry_seconds:g}s.",
    )


async def _try_restore_runtime_state(
    state: _InstanceRuntime,
    *,
    account_fingerprint: str,
    execute: bool,
    now: float,
) -> bool:
    try:
        restored = await _restore_runtime_state(
            state,
            account_fingerprint=account_fingerprint,
            execute=execute,
            now=now,
        )
    except ApiError as error:
        if state.journal is None:
            if not error.is_transient_response:
                raise
            _schedule_position_recovery_retry(state, error=error, now=now)
            return False
        if not is_retryable_cleanup_error(error):
            raise
        _schedule_cleanup_retry(state, error=error, now=now)
        return False
    return restored


async def _cleanup_or_schedule_retry(
    state: _InstanceRuntime,
    *,
    execute: bool,
    now: float,
) -> bool:
    try:
        await _cleanup_runtime_state(state, execute=execute)
    except ApiError as error:
        if not is_retryable_cleanup_error(error):
            raise
        _schedule_cleanup_retry(state, error=error, now=now)
        return False
    return True


async def _pause_runtime_state(
    state: _InstanceRuntime,
    *,
    status: str,
    message: str,
    execute: bool,
    now: float,
) -> None:
    first_failure = state.market_data_paused_at is None
    if first_failure:
        state.market_data_paused_at = now
    state.status = status
    state.failure_reason = status
    state.next_market_data_retry_at = now + state.config.trading.market_data_retry_seconds
    if state.event_log is not None:
        state.event_log.write(
            "MARKET_DATA_PAUSED" if first_failure else "MARKET_DATA_RETRY_FAILED",
            status=status,
            message=message,
            retry_seconds=state.config.trading.market_data_retry_seconds,
        )
    _write_console_status(
        f"Strategy:{state.config.name}",
        f"Market data paused status={status}: {message} "
        f"Retrying in {state.config.trading.market_data_retry_seconds:g}s.",
    )
    if (
        state.journal is not None
        and state.market_data_paused_at is not None
        and now - state.market_data_paused_at >= state.config.trading.stale_position_grace_seconds
    ):
        _write_console_status(
            f"Strategy:{state.config.name}",
            "Market data grace period expired; cleaning up the owned position.",
        )
        await _cleanup_or_schedule_retry(state, execute=execute, now=now)


def _recover_runtime_state(state: _InstanceRuntime, *, now: float) -> None:
    if state.market_data_paused_at is None:
        return
    paused_seconds = round(now - state.market_data_paused_at, 3)
    if state.event_log is not None:
        state.event_log.write("MARKET_DATA_RECOVERED", paused_seconds=paused_seconds)
    _write_console_status(
        f"Strategy:{state.config.name}",
        f"Market data recovered after {paused_seconds:g}s; strategy resumed.",
    )
    state.market_data_paused_at = None
    state.next_market_data_retry_at = 0.0
    state.failure_reason = None
    state.status = "streaming"


async def algo_runner(config: AppConfig, mode: str, execute: bool) -> CommandResult:
    if mode == "live-execute":
        assert_live_execution_enabled(enabled=config.live_trading_enabled, execute=execute)
    elif execute:
        raise LiveExecutionBlocked("--execute is only valid with --mode live-execute.")

    loop = asyncio.get_running_loop()
    fingerprint = account_fingerprint(config.secrets.api_key)
    states = [
        _InstanceRuntime(
            config=instance,
            deadline=(
                None
                if instance.trading.max_runtime_seconds is None
                else loop.time() + instance.trading.max_runtime_seconds
            ),
            event_log=InstanceEventLog(instance.name),
        )
        for instance in config.algo_instances
    ]
    heartbeat = HeartbeatWriter()
    position_update_queue: asyncio.Queue[PositionUpdateNotification] = asyncio.Queue()
    position_stream_stop = asyncio.Event()
    position_stream_task: asyncio.Task[None] | None = None
    position_reconcile_throttle = PositionReconcileThrottle()

    _write_console_status(
        "Runner",
        f"Starting mode={mode} environment={config.environment} instances={len(states)} "
        f"account={fingerprint}",
    )

    for state in states:
        instance = state.config
        assert state.event_log is not None
        state.event_log.write(
            "STARTED",
            mode=mode,
            environment=config.environment,
            contract=instance.trading.contract,
            amount=instance.trading.amount,
            strategy=instance.strategy.name,
            period_type=instance.strategy.period_type,
        )
        _write_console_status(
            f"Strategy:{instance.name}",
            f"Initialized contract={instance.trading.contract} "
            f"strategy={instance.strategy.name} period_type={instance.strategy.period_type} "
            f"amount={instance.trading.amount}",
        )

    try:
        async with make_client(config, require_chart=True) as client:
            settings = await client.get_contract_settings()
            for state in states:
                setting = find_contract_setting(settings, state.config.trading.contract)
                validate_amount(amount=state.config.trading.amount, contract_setting=setting)
                _write_console_status(
                    f"REST:{state.config.name}",
                    f"Contract settings validated for {state.config.trading.contract}",
                )
                if mode == "live-execute":
                    state.manager = ExecutionManager(
                        client=client,
                        journal_path=instance_journal_path(
                            state.config.name, state.config.trading.contract
                        ),
                        live_trading_enabled=config.live_trading_enabled,
                        poll_seconds=min(1.0, state.config.trading.poll_seconds),
                        cleanup_attempts=1,
                    )
                    await _try_restore_runtime_state(
                        state,
                        account_fingerprint=fingerprint,
                        execute=execute,
                        now=loop.time(),
                    )

            async with PriceStreamSession(config) as price_session:
                _write_console_status("Price", "Shared price session connected")
                if mode == "live-execute":
                    _write_console_status("Position", "Shared position event stream connecting")
                    position_stream_task = asyncio.create_task(
                        _run_position_event_stream(
                            client,
                            position_update_queue,
                            position_stream_stop,
                        )
                    )
                for state in states:
                    assert state.event_log is not None
                    state.event_log.write("PRICE_SESSION_CONNECTED")
                try:
                    pending_position_refs: set[str] = set()
                    force_position_reconcile = False
                    while any(not state.completed for state in states):
                        while True:
                            try:
                                notification = position_update_queue.get_nowait()
                            except asyncio.QueueEmpty:
                                break
                            if notification.affected_refs:
                                pending_position_refs.update(notification.affected_refs)
                            else:
                                force_position_reconcile = True
                        for state in states:
                            if state.completed:
                                continue
                            instance = state.config
                            now = loop.time()
                            if state.status == "position-recovery-pending":
                                if now < state.next_position_recovery_retry_at:
                                    await asyncio.sleep(0.05)
                                    continue
                                await _try_restore_runtime_state(
                                    state,
                                    account_fingerprint=fingerprint,
                                    execute=execute,
                                    now=loop.time(),
                                )
                                if state.status in {
                                    "position-recovery-pending",
                                    "cleanup-pending",
                                }:
                                    continue
                            if state.status == "cleanup-pending":
                                if now < state.next_cleanup_retry_at:
                                    continue
                                if not await _cleanup_or_schedule_retry(
                                    state,
                                    execute=execute,
                                    now=loop.time(),
                                ):
                                    continue
                                state.next_cleanup_retry_at = 0.0
                                state.status = "cleanup-reconciled"
                            if state.journal is not None:
                                deal_ref = state.journal.order_ref
                                if deal_ref is not None:
                                    await _reconcile_external_position(
                                        state,
                                        client=client,
                                        throttle=position_reconcile_throttle,
                                        now=now,
                                        force=(
                                            force_position_reconcile
                                            or deal_ref in pending_position_refs
                                        ),
                                    )
                                    if (
                                        force_position_reconcile
                                        or deal_ref in pending_position_refs
                                    ):
                                        pending_position_refs.discard(deal_ref)
                            if state.deadline is not None and now >= state.deadline:
                                if not await _cleanup_or_schedule_retry(
                                    state,
                                    execute=execute,
                                    now=loop.time(),
                                ):
                                    continue
                                if state.market_data_paused_at is None:
                                    state.status = "runtime-limit"
                                state.completed = True
                                continue
                            if (
                                state.market_data_paused_at is not None
                                and now < state.next_market_data_retry_at
                            ):
                                if (
                                    state.journal is not None
                                    and now - state.market_data_paused_at
                                    >= instance.trading.stale_position_grace_seconds
                                ):
                                    _write_console_status(
                                        f"Strategy:{instance.name}",
                                        "Market data grace period expired; cleaning up the "
                                        "owned position.",
                                    )
                                    if not await _cleanup_or_schedule_retry(
                                        state,
                                        execute=execute,
                                        now=loop.time(),
                                    ):
                                        continue
                                continue

                            try:
                                state.last_quote = await price_session.get_quote(
                                    instance.trading.contract,
                                    timeout_seconds=max(
                                        1.0, min(10.0, instance.trading.poll_seconds * 2)
                                    ),
                                )
                            except QuoteUnavailableError as error:
                                await _pause_runtime_state(
                                    state,
                                    status="stale-quote",
                                    message=str(error),
                                    execute=execute,
                                    now=loop.time(),
                                )
                                continue
                            quote_received_at = loop.time()
                            if quote_received_at >= state.next_price_log_at:
                                _write_console_status(
                                    f"Price:{instance.name}",
                                    f"{instance.trading.contract} bid={state.last_quote.bid} "
                                    f"ask={state.last_quote.ask} "
                                    f"tag={'yes' if state.last_quote.tag else 'no'}",
                                )
                                state.next_price_log_at = (
                                    quote_received_at + instance.trading.price_log_interval_seconds
                                )
                            state.status = "streaming"
                            try:
                                bars = await client.get_completed_bars(
                                    contract=instance.trading.contract,
                                    period_type=instance.strategy.period_type,
                                    count=instance.trading.bar_count,
                                )
                            except ApiError as error:
                                if not error.is_transient_response:
                                    raise
                                await _pause_runtime_state(
                                    state,
                                    status="chart-unavailable",
                                    message=str(error),
                                    execute=execute,
                                    now=loop.time(),
                                )
                                continue
                            latest_bar_age = _latest_bar_age_seconds(bars)
                            stale_threshold = _stale_threshold_seconds(
                                instance.strategy.period_type,
                                instance.trading.bar_stale_grace_seconds,
                            )
                            if _latest_is_stale_for_period(
                                bars,
                                instance.strategy.period_type,
                                instance.trading.bar_stale_grace_seconds,
                            ):
                                freshness = (
                                    "missing latest completed bar"
                                    if latest_bar_age is None
                                    else (
                                        f"latest_age_seconds={latest_bar_age:.1f} "
                                        f"threshold_seconds={stale_threshold:.1f}"
                                    )
                                )
                                await _pause_runtime_state(
                                    state,
                                    status="stale-bars",
                                    message=(
                                        "Completed market data is stale or missing for "
                                        f"{instance.name} ({freshness})."
                                    ),
                                    execute=execute,
                                    now=loop.time(),
                                )
                                continue
                            bars, gap = _contiguous_bar_suffix(bars, instance.strategy.period_type)
                            if gap is not None and len(bars) < required_completed_bars(
                                instance.strategy
                            ):
                                await _pause_runtime_state(
                                    state,
                                    status="bar-gap",
                                    message=_bar_gap_message(instance.name, gap, len(bars)),
                                    execute=execute,
                                    now=loop.time(),
                                )
                                continue
                            if len(bars) < required_completed_bars(instance.strategy):
                                await _pause_runtime_state(
                                    state,
                                    status="insufficient-bars",
                                    message=f"Not enough completed bars for {instance.name}.",
                                    execute=execute,
                                    now=loop.time(),
                                )
                                continue
                            _recover_runtime_state(state, now=loop.time())
                            if not state.history_loaded:
                                assert state.event_log is not None
                                state.event_log.write(
                                    "HISTORY_LOADED",
                                    count=len(bars),
                                    period_type=instance.strategy.period_type,
                                    first_time_ms=bars[0].time_ms,
                                    latest_time_ms=bars[-1].time_ms,
                                )
                                _write_console_status(
                                    f"Chart:{instance.name}",
                                    f"Historical candles loaded count={len(bars)} "
                                    f"period_type={instance.strategy.period_type} "
                                    f"first_time_ms={bars[0].time_ms} "
                                    f"latest_time_ms={bars[-1].time_ms}",
                                )
                                state.history_loaded = True
                            if bars[-1].time_ms == state.last_bar_time:
                                continue
                            state.last_bar_time = bars[-1].time_ms
                            evaluation = evaluate_latest_strategy(
                                bars,
                                strategy=instance.strategy,
                                position_side=state.owned_side,
                            )
                            signal_name = (
                                evaluation.event.signal.value
                                if evaluation.event
                                else Signal.NONE.value
                            )
                            position_name = state.owned_side.value if state.owned_side else "FLAT"
                            _write_console_status(
                                f"Strategy:{instance.name}",
                                f"Indicators calculated candle_time_ms={bars[-1].time_ms} "
                                f"close={bars[-1].close} "
                                f"indicators={_indicator_summary(evaluation.indicators)} "
                                f"signal={signal_name} position={position_name}",
                            )
                            assert state.event_log is not None
                            state.event_log.write(
                                "BAR_EVALUATED",
                                bar_time_ms=bars[-1].time_ms,
                                close=bars[-1].close,
                                bid=state.last_quote.bid,
                                ask=state.last_quote.ask,
                                tag_present=bool(state.last_quote.tag),
                                position=(state.owned_side.value if state.owned_side else None),
                                indicators=evaluation.indicators,
                            )
                            event = evaluation.event
                            if event is None:
                                state.status = "waiting-signal"
                                continue
                            state.events.append(event)
                            _write_console_status(
                                f"Strategy:{instance.name}",
                                f"Signal confirmed: {event.signal.value} "
                                f"bid={state.last_quote.bid} ask={state.last_quote.ask}",
                            )
                            state.event_log.write(
                                "SIGNAL",
                                signal=event.signal.value,
                                bar_time_ms=event.time_ms,
                                close=event.close,
                                indicators=evaluation.indicators,
                            )
                            if mode == "live-observe":
                                state.status = "signal-observed"
                                continue

                            manager = state.manager
                            if manager is None:
                                raise RuntimeError("Execution manager was not initialized.")
                            if event.signal in {Signal.OPEN_BUY, Signal.OPEN_SELL}:
                                if state.owned_side is not None:
                                    continue
                                _write_instance_execution_summary(config, state, fingerprint, event)
                                client_order_id = _client_order_id()
                                side = "BUY" if event.signal is Signal.OPEN_BUY else "SELL"
                                state.event_log.write(
                                    "ORDER_SUBMITTED",
                                    side=side,
                                    amount=instance.trading.amount,
                                    client_order_id=client_order_id,
                                )
                                _write_console_status(
                                    f"REST:{instance.name}",
                                    f"Submitting {side} {instance.trading.contract} "
                                    f"amount={instance.trading.amount} "
                                    f"client_order_id={client_order_id}",
                                )
                                try:
                                    state.journal = await manager.open_position(
                                        execute=execute,
                                        account_fingerprint=fingerprint,
                                        contract=instance.trading.contract,
                                        amount=instance.trading.amount,
                                        buy=event.signal is Signal.OPEN_BUY,
                                        client_order_id=client_order_id,
                                    )
                                except ApiError as error:
                                    if not error.is_definitive_rejection:
                                        raise
                                    state.status = "trade-rejected"
                                    state.failure_reason = str(error)
                                    state.completed = True
                                    state.event_log.write(
                                        "ORDER_REJECTED",
                                        side=side,
                                        amount=instance.trading.amount,
                                        client_order_id=client_order_id,
                                        status_code=error.status_code,
                                        message=str(error),
                                    )
                                    _write_console_status(
                                        f"REST:{instance.name}",
                                        f"Order rejected; instance disabled: {error} "
                                        "Other instances continue.",
                                    )
                                    continue
                                state.owned_side = (
                                    PositionSide.LONG
                                    if event.signal is Signal.OPEN_BUY
                                    else PositionSide.SHORT
                                )
                                state.event_log.write(
                                    "POSITION_CONFIRMED",
                                    deal_ref=state.journal.order_ref,
                                    side=side,
                                    amount=instance.trading.amount,
                                )
                                _write_console_status(
                                    f"REST:{instance.name}",
                                    f"Position confirmed deal_ref={state.journal.order_ref}",
                                )
                                state.next_position_reconcile_at = (
                                    loop.time() + instance.trading.position_reconcile_seconds
                                )
                                state.status = "position-open"
                            elif state.journal is not None:
                                if not await _cleanup_or_schedule_retry(
                                    state,
                                    execute=execute,
                                    now=loop.time(),
                                ):
                                    continue
                                state.status = "waiting-signal"
                                state.event_log.write("ROUND_TRIP_COMPLETED")
                                _write_console_status(
                                    f"Strategy:{instance.name}",
                                    "Round trip complete; waiting for the next signal.",
                                )

                        force_position_reconcile = False
                        heartbeat.write({state.config.name: state.status for state in states})
                        active_polls = [
                            state.config.trading.poll_seconds
                            for state in states
                            if not state.completed
                        ]
                        if active_polls:
                            await asyncio.sleep(min(active_polls))
                finally:
                    if position_stream_task is not None:
                        position_stream_stop.set()
                        position_stream_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await position_stream_task
                    primary_error = sys.exception()
                    for state in states:
                        if state.journal is not None:
                            try:
                                await _cleanup_runtime_state(state, execute=execute)
                                state.status = "cleaned-up"
                            except Exception as cleanup_error:
                                if primary_error is None:
                                    raise
                                state.status = "cleanup-deferred"
                                if state.event_log is not None:
                                    state.event_log.write(
                                        "CLEANUP_DEFERRED",
                                        deal_ref=state.journal.order_ref,
                                        error_type=type(cleanup_error).__name__,
                                        message=str(cleanup_error),
                                    )
                                _write_console_status(
                                    f"REST:{state.config.name}",
                                    f"Shutdown cleanup deferred: {cleanup_error} "
                                    "The ownership journal was retained.",
                                )
                    heartbeat.write({state.config.name: state.status for state in states})
    except Exception as error:
        _write_console_status("Runner", f"Stopped by {type(error).__name__}: {error}")
        for state in states:
            if state.event_log is not None:
                state.event_log.write(
                    "ERROR",
                    status=state.status,
                    error_type=type(error).__name__,
                    message=str(error),
                )
        raise
    finally:
        for state in states:
            if state.event_log is not None:
                state.event_log.write("STOPPED", status=state.status)
                state.event_log.close()
            _write_console_status(f"Strategy:{state.config.name}", f"Stopped status={state.status}")

    failed_instances = [state.config.name for state in states if state.failure_reason is not None]
    has_events = any(state.events for state in states)
    outcome = (
        Outcome.INCONCLUSIVE
        if failed_instances
        else (Outcome.SUCCESS if has_events else Outcome.NO_SIGNAL)
    )
    return CommandResult(
        "algo-runner",
        outcome,
        (
            "Algo runner completed with one or more disabled instances."
            if failed_instances
            else "Algo runner stopped at its configured runtime limit."
        ),
        {
            "mode": mode,
            "account_fingerprint": fingerprint,
            "shared_price_session": True,
            "failed_instances": failed_instances,
            "instances": {state.config.name: _runtime_data(state) for state in states},
        },
    )


async def recover(config: AppConfig, execute: bool) -> CommandResult:
    candidate_paths = [_journal_path(config)] + [
        instance_journal_path(instance.name, instance.trading.contract)
        for instance in config.algo_instances
    ]
    paths = list(dict.fromkeys(path for path in candidate_paths if path.exists()))
    if not paths:
        return CommandResult(
            "recover", Outcome.SUCCESS, "No unresolved execution journal exists.", {}
        )
    fingerprint = account_fingerprint(config.secrets.api_key)
    results: list[dict[str, Any]] = []
    blocked = False
    async with make_client(config) as client:
        manager_by_path: dict[Path, ExecutionManager] = {}
        for path in paths:
            journal = Journal.load(path)
            if journal.account_fingerprint != fingerprint:
                raise LiveExecutionBlocked(
                    "Recovery journal belongs to a different account fingerprint."
                )
            if not journal.order_ref:
                manager = manager_by_path.setdefault(
                    path,
                    ExecutionManager(
                        client=client,
                        journal_path=path,
                        live_trading_enabled=config.live_trading_enabled,
                        poll_seconds=config.trading.market_data_retry_seconds,
                    ),
                )
                reconciled = await manager.reconcile_submission(journal=journal)
                if reconciled is None:
                    journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
                    blocked = True
                    results.append(
                        {
                            "journal": str(path),
                            "state": JournalState.OWNERSHIP_UNCONFIRMED.value,
                            "client_order_id": journal.client_order_id,
                        }
                    )
                    continue
                journal = reconciled
            if journal.order_ref is None:
                raise LiveExecutionBlocked(
                    f"Execution journal for {path} has no position reference after reconciliation."
                )
            position = await client.get_position_detail(journal.order_ref)
            if position is None:
                journal.clear()
                results.append({"journal": str(path), "state": "already-closed"})
                continue
            if not execute:
                blocked = True
                results.append(
                    {
                        "journal": str(path),
                        "state": "open",
                        "order_ref": journal.order_ref,
                    }
                )
                continue
            manager = manager_by_path.setdefault(
                path,
                ExecutionManager(
                    client=client,
                    journal_path=path,
                    live_trading_enabled=config.live_trading_enabled,
                ),
            )
            cleanup_ref = await manager.cleanup(
                journal=journal, execute=True, client_order_id=_client_order_id()
            )
            results.append(
                {"journal": str(path), "state": "cleaned-up", "cleanup_ref": cleanup_ref}
            )
    return CommandResult(
        "recover",
        Outcome.BLOCKED if blocked else Outcome.SUCCESS,
        (
            "One or more journals require inspection or both execution gates."
            if blocked
            else "Tracked positions were reconciled."
        ),
        {"journals": results},
    )
