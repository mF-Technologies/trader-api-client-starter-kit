from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class LiveExecutionBlocked(RuntimeError):
    """Raised when a trading mutation has not passed all safety gates."""


class JournalState(StrEnum):
    PENDING_SUBMISSION = "PENDING_SUBMISSION"
    OPEN = "OPEN"
    CLEANUP_PENDING = "CLEANUP_PENDING"
    OWNERSHIP_UNCONFIRMED = "OWNERSHIP_UNCONFIRMED"


def assert_live_execution_enabled(*, enabled: bool, execute: bool) -> None:
    if not enabled or not execute:
        raise LiveExecutionBlocked(
            "Live execution requires TRADER_API_ENABLE_LIVE_TRADING=true and --execute."
        )


@dataclass(frozen=True)
class Journal:
    path: Path
    run_id: str
    account_fingerprint: str
    contract: str
    side: str
    amount: float
    client_order_id: int
    state: JournalState
    updated_at: str
    order_ref: str | None = None
    cleanup_ref: str | None = None
    cleanup_client_order_id: int | None = None
    entry_time_ms: int | None = None
    stop_price: float | None = None
    entry_price: float | None = None
    entry_equity: float | None = None

    @classmethod
    def begin_submission(
        cls,
        *,
        path: Path,
        run_id: str,
        account_fingerprint: str,
        contract: str,
        side: str,
        amount: float,
        client_order_id: int,
        entry_time_ms: int | None = None,
        stop_price: float | None = None,
        entry_price: float | None = None,
        entry_equity: float | None = None,
    ) -> Journal:
        cls.assert_clear(path)
        journal = cls(
            path=path,
            run_id=run_id,
            account_fingerprint=account_fingerprint,
            contract=contract,
            side=side,
            amount=amount,
            client_order_id=client_order_id,
            state=JournalState.PENDING_SUBMISSION,
            updated_at=datetime.now(UTC).isoformat(),
            entry_time_ms=entry_time_ms,
            stop_price=stop_price,
            entry_price=entry_price,
            entry_equity=entry_equity,
        )
        journal.save()
        return journal

    @classmethod
    def load(cls, path: Path) -> Journal:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path=path,
            run_id=str(data["run_id"]),
            account_fingerprint=str(data["account_fingerprint"]),
            contract=str(data["contract"]),
            side=str(data["side"]),
            amount=float(data["amount"]),
            client_order_id=int(data["client_order_id"]),
            state=JournalState(data["state"]),
            updated_at=str(data["updated_at"]),
            order_ref=str(data["order_ref"]) if data.get("order_ref") else None,
            cleanup_ref=str(data["cleanup_ref"]) if data.get("cleanup_ref") else None,
            cleanup_client_order_id=(
                int(data["cleanup_client_order_id"])
                if data.get("cleanup_client_order_id")
                else None
            ),
            entry_time_ms=(
                int(data["entry_time_ms"])
                if data.get("entry_time_ms") is not None
                else None
            ),
            stop_price=(float(data["stop_price"]) if data.get("stop_price") is not None else None),
            entry_price=(
                float(data["entry_price"]) if data.get("entry_price") is not None else None
            ),
            entry_equity=(
                float(data["entry_equity"]) if data.get("entry_equity") is not None else None
            ),
        )

    @staticmethod
    def assert_clear(path: Path) -> None:
        if path.exists():
            raise LiveExecutionBlocked(
                f"An unresolved execution journal blocks new submissions: {path}"
            )

    def with_state(self, state: JournalState, **updates: Any) -> Journal:
        values = asdict(self)
        values.update(updates)
        values["path"] = self.path
        values["state"] = state
        values["updated_at"] = datetime.now(UTC).isoformat()
        journal = Journal(**values)
        journal.save()
        return journal

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload.pop("path")
        payload["state"] = self.state.value
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


@dataclass(frozen=True)
class LiveRiskState:
    """Persisted account-level risk state used by the live SMA loop."""

    path: Path
    account_fingerprint: str
    contract: str
    utc_date: str
    daily_start_equity: float
    peak_equity: float
    daily_loss_triggered: bool
    drawdown_triggered: bool
    updated_at: str

    @classmethod
    def initialize(
        cls,
        *,
        path: Path,
        account_fingerprint: str,
        contract: str,
        utc_date: str,
        equity: float,
    ) -> LiveRiskState:
        state = cls(
            path=path,
            account_fingerprint=account_fingerprint,
            contract=contract,
            utc_date=utc_date,
            daily_start_equity=equity,
            peak_equity=equity,
            daily_loss_triggered=False,
            drawdown_triggered=False,
            updated_at=datetime.now(UTC).isoformat(),
        )
        state.save()
        return state

    @classmethod
    def load(cls, path: Path) -> LiveRiskState:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path=path,
            account_fingerprint=str(data["account_fingerprint"]),
            contract=str(data["contract"]),
            utc_date=str(data["utc_date"]),
            daily_start_equity=float(data["daily_start_equity"]),
            peak_equity=float(data["peak_equity"]),
            daily_loss_triggered=bool(data["daily_loss_triggered"]),
            drawdown_triggered=bool(data["drawdown_triggered"]),
            updated_at=str(data["updated_at"]),
        )

    def with_updates(self, **updates: Any) -> LiveRiskState:
        values = asdict(self)
        values.update(updates)
        values["path"] = self.path
        values["updated_at"] = datetime.now(UTC).isoformat()
        state = LiveRiskState(**values)
        state.save()
        return state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        payload.pop("path")
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
