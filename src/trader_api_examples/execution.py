from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .api import ApiError
from .safety import Journal, JournalState, LiveExecutionBlocked, assert_live_execution_enabled

RETRYABLE_CLEANUP_ERROR_CODES = {"710", "934"}


def is_retryable_cleanup_error(error: ApiError) -> bool:
    return error.error_code in RETRYABLE_CLEANUP_ERROR_CODES or error.is_transient_response


class ExecutionClient(Protocol):
    async def add_market_deal(
        self,
        *,
        contract: str,
        amount: float,
        buy: bool,
        client_order_id: int,
    ) -> str | None: ...

    async def get_positions(self) -> list[dict[str, Any]]: ...

    async def reconcile_market_deal(
        self,
        *,
        contract: str,
        amount: float,
        buy: bool,
        client_order_id: int,
        excluded_refs: tuple[str, ...],
    ) -> str | None: ...

    async def get_position_detail(self, order_ref: str) -> dict[str, Any] | None: ...

    async def liquidate_market_deal(
        self, *, order_ref: str, amount: float, client_order_id: int
    ) -> str | None: ...


class ExecutionManager:
    def __init__(
        self,
        *,
        client: ExecutionClient,
        journal_path: Path,
        live_trading_enabled: bool,
        poll_seconds: float = 1.0,
        confirmation_attempts: int = 10,
        cleanup_attempts: int = 5,
        client_order_id_factory: Callable[[], int] | None = None,
    ) -> None:
        self.client = client
        self.journal_path = journal_path
        self.live_trading_enabled = live_trading_enabled
        self.poll_seconds = poll_seconds
        self.confirmation_attempts = confirmation_attempts
        self.cleanup_attempts = cleanup_attempts
        self.client_order_id_factory = client_order_id_factory or (
            lambda: secrets.randbelow(2_147_483_646) + 1
        )

    async def open_position(
        self,
        *,
        execute: bool,
        account_fingerprint: str,
        contract: str,
        amount: float,
        buy: bool,
        client_order_id: int,
    ) -> Journal:
        assert_live_execution_enabled(enabled=self.live_trading_enabled, execute=execute)
        journal = Journal.begin_submission(
            path=self.journal_path,
            run_id=str(uuid4()),
            account_fingerprint=account_fingerprint,
            contract=contract,
            side="BUY" if buy else "SELL",
            amount=amount,
            client_order_id=client_order_id,
        )
        try:
            preexisting_positions = await self.client.get_positions()
        except Exception:
            journal.clear()
            raise
        journal = journal.with_state(
            JournalState.PENDING_SUBMISSION,
            preexisting_position_refs=tuple(
                ref
                for ref in (_record_ref(position) for position in preexisting_positions)
                if ref is not None
            ),
        )
        try:
            order_ref = await self.client.add_market_deal(
                contract=contract,
                amount=amount,
                buy=buy,
                client_order_id=client_order_id,
            )
        except ApiError as error:
            if error.is_definitive_rejection:
                journal.clear()
            else:
                journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
            raise
        except Exception:
            journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
            raise
        if order_ref is None:
            reconciled = await self.reconcile_submission(journal=journal)
            if reconciled is None:
                journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
                raise ApiError(
                    "addDeal did not return dealRef and no unique open position "
                    "could be reconciled."
                )
            journal = reconciled
            order_ref = journal.order_ref
        else:
            journal = journal.with_state(JournalState.OPEN, order_ref=order_ref)
        if order_ref is None:
            journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
            raise RuntimeError("Reconciliation returned no position reference.")
        if not await self._wait_for_position(order_ref, present=True):
            journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
            raise RuntimeError("addDeal returned a reference but the position was not confirmed.")
        return journal

    async def reconcile_submission(self, *, journal: Journal) -> Journal | None:
        """Resolve a submission whose response did not contain a usable deal reference."""
        for attempt in range(self.confirmation_attempts):
            try:
                order_ref = await self.client.reconcile_market_deal(
                    contract=journal.contract,
                    amount=journal.amount,
                    buy=journal.side == "BUY",
                    client_order_id=journal.client_order_id,
                    excluded_refs=journal.preexisting_position_refs,
                )
            except ApiError as error:
                if not error.is_transient_response:
                    raise
                order_ref = None
            if (
                order_ref is not None
                and await self.client.get_position_detail(order_ref) is not None
            ):
                return journal.with_state(JournalState.OPEN, order_ref=order_ref)
            if attempt + 1 < self.confirmation_attempts:
                await asyncio.sleep(self.poll_seconds)
        return None

    async def cleanup(self, *, journal: Journal, execute: bool, client_order_id: int) -> str:
        assert_live_execution_enabled(enabled=self.live_trading_enabled, execute=execute)
        if journal.path.exists():
            persisted = Journal.load(journal.path)
            if persisted.run_id != journal.run_id:
                raise LiveExecutionBlocked(
                    "Recovery journal changed before cleanup; refusing to submit."
                )
            journal = persisted
        order_ref = journal.order_ref
        if not order_ref:
            journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
            raise RuntimeError("Cannot clean up a position without a confirmed order reference.")
        cleanup_client_order_id = journal.cleanup_client_order_id or client_order_id
        journal = journal.with_state(
            JournalState.CLEANUP_PENDING,
            cleanup_client_order_id=cleanup_client_order_id,
        )
        cleanup_ref: str | None = None
        for attempt in range(self.cleanup_attempts):
            try:
                cleanup_ref = await self.client.liquidate_market_deal(
                    order_ref=order_ref,
                    amount=journal.amount,
                    client_order_id=cleanup_client_order_id,
                )
                if cleanup_ref is None:
                    if await self._wait_for_position(order_ref, present=False):
                        cleanup_ref = f"position-confirmed:{cleanup_client_order_id}"
                        break
                    raise ApiError(
                        "liquidate returned no reference and the position remains open.",
                        503,
                    )
                break
            except ApiError as error:
                if error.is_duplicate_client_order_id:
                    if await self._wait_for_position(order_ref, present=False):
                        cleanup_ref = f"duplicate-confirmed:{cleanup_client_order_id}"
                        break
                    raise ApiError(
                        "liquidate request was already accepted, but the position remains "
                        "open; awaiting confirmation.",
                        503,
                    ) from error
                if not is_retryable_cleanup_error(error) or attempt + 1 >= self.cleanup_attempts:
                    raise
                cleanup_client_order_id = self.client_order_id_factory()
                journal = journal.with_state(
                    JournalState.CLEANUP_PENDING,
                    cleanup_client_order_id=cleanup_client_order_id,
                )
                await asyncio.sleep(self.poll_seconds)
        if cleanup_ref is None:
            raise RuntimeError("Liquidation did not produce a confirmed result.")
        journal = journal.with_state(JournalState.CLEANUP_PENDING, cleanup_ref=cleanup_ref)
        if not await self._wait_for_position(order_ref, present=False):
            raise RuntimeError("Cleanup was accepted but zero position was not confirmed.")
        journal.clear()
        return cleanup_ref

    async def _wait_for_position(self, order_ref: str, *, present: bool) -> bool:
        for attempt in range(self.confirmation_attempts):
            position = await self.client.get_position_detail(order_ref)
            if (position is not None) is present:
                return True
            if attempt + 1 < self.confirmation_attempts:
                await asyncio.sleep(self.poll_seconds)
        return False


def _record_ref(record: dict[str, Any]) -> str | None:
    for key in ("ref", "orderNo", "dealRef"):
        value = record.get(key)
        if value is not None and str(value):
            return str(value)
    return None
