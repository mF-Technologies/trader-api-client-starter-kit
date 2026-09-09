from __future__ import annotations

import asyncio
from math import isclose
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from .safety import Journal, JournalState, assert_live_execution_enabled


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
    ) -> None:
        self.client = client
        self.journal_path = journal_path
        self.live_trading_enabled = live_trading_enabled
        self.poll_seconds = poll_seconds
        self.confirmation_attempts = confirmation_attempts

    async def open_position(
        self,
        *,
        execute: bool,
        account_fingerprint: str,
        contract: str,
        amount: float,
        buy: bool,
        client_order_id: int,
        entry_time_ms: int | None = None,
        stop_price: float | None = None,
        entry_price: float | None = None,
        entry_equity: float | None = None,
    ) -> Journal:
        assert_live_execution_enabled(enabled=self.live_trading_enabled, execute=execute)
        positions_before = await self.client.get_positions()
        journal = Journal.begin_submission(
            path=self.journal_path,
            run_id=str(uuid4()),
            account_fingerprint=account_fingerprint,
            contract=contract,
            side="BUY" if buy else "SELL",
            amount=amount,
            client_order_id=client_order_id,
            entry_time_ms=entry_time_ms,
            stop_price=stop_price,
            entry_price=entry_price,
            entry_equity=entry_equity,
        )
        try:
            order_ref = await self.client.add_market_deal(
                contract=contract,
                amount=amount,
                buy=buy,
                client_order_id=client_order_id,
            )
        except Exception:
            journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
            raise
        resolved_order_ref = order_ref or await self._discover_position_reference(
            positions_before=positions_before,
            contract=contract,
            amount=amount,
        )
        if not resolved_order_ref:
            journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
            raise RuntimeError("addDeal succeeded but no unique position reference was confirmed.")
        journal = journal.with_state(JournalState.OPEN, order_ref=resolved_order_ref)
        if not await self._wait_for_position(resolved_order_ref, present=True):
            journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
            raise RuntimeError("addDeal succeeded but the position was not confirmed.")
        return journal

    async def cleanup(self, *, journal: Journal, execute: bool, client_order_id: int) -> str | None:
        assert_live_execution_enabled(enabled=self.live_trading_enabled, execute=execute)
        order_ref = journal.order_ref
        if not order_ref:
            journal.with_state(JournalState.OWNERSHIP_UNCONFIRMED)
            raise RuntimeError("Cannot clean up a position without a confirmed order reference.")
        cleanup_client_order_id = journal.cleanup_client_order_id or client_order_id
        journal = journal.with_state(
            JournalState.CLEANUP_PENDING,
            cleanup_client_order_id=cleanup_client_order_id,
        )
        cleanup_ref = await self.client.liquidate_market_deal(
            order_ref=order_ref,
            amount=journal.amount,
            client_order_id=cleanup_client_order_id,
        )
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

    async def _discover_position_reference(
        self,
        *,
        positions_before: list[dict[str, Any]],
        contract: str,
        amount: float,
    ) -> str | None:
        existing_refs = {
            reference
            for position in positions_before
            if (reference := self._position_reference(position)) is not None
        }
        for attempt in range(self.confirmation_attempts):
            positions = await self.client.get_positions()
            candidates = [
                reference
                for position in positions
                if (reference := self._position_reference(position)) is not None
                and reference not in existing_refs
                and self._position_matches(position, contract=contract, amount=amount)
            ]
            if len(candidates) == 1:
                return candidates[0]
            if attempt + 1 < self.confirmation_attempts:
                await asyncio.sleep(self.poll_seconds)
        return None

    @staticmethod
    def _position_reference(position: dict[str, Any]) -> str | None:
        reference = position.get("ref") or position.get("orderRef")
        return str(reference) if reference is not None else None

    @staticmethod
    def _position_matches(position: dict[str, Any], *, contract: str, amount: float) -> bool:
        position_contract = (
            position.get("contract") or position.get("contractCode") or position.get("market")
        )
        raw_amount = position.get("amount")
        if position_contract is None or raw_amount is None:
            return False
        try:
            position_amount = float(raw_amount)
        except (TypeError, ValueError):
            return False
        return str(position_contract) == contract and isclose(
            position_amount, amount, rel_tol=1e-9, abs_tol=1e-9
        )
