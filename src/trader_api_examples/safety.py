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
    submitted_at: str = ""
    preexisting_position_refs: tuple[str, ...] = ()
    order_ref: str | None = None
    cleanup_ref: str | None = None
    cleanup_client_order_id: int | None = None

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
        preexisting_position_refs: tuple[str, ...] = (),
    ) -> Journal:
        cls.assert_clear(path)
        submitted_at = datetime.now(UTC).isoformat()
        journal = cls(
            path=path,
            run_id=run_id,
            account_fingerprint=account_fingerprint,
            contract=contract,
            side=side,
            amount=amount,
            client_order_id=client_order_id,
            state=JournalState.PENDING_SUBMISSION,
            updated_at=submitted_at,
            submitted_at=submitted_at,
            preexisting_position_refs=preexisting_position_refs,
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
            submitted_at=str(data.get("submitted_at") or data["updated_at"]),
            preexisting_position_refs=tuple(
                str(ref)
                for ref in data.get("preexisting_position_refs", [])
                if ref is not None and str(ref)
            ),
            order_ref=str(data["order_ref"]) if data.get("order_ref") else None,
            cleanup_ref=str(data["cleanup_ref"]) if data.get("cleanup_ref") else None,
            cleanup_client_order_id=(
                int(data["cleanup_client_order_id"])
                if data.get("cleanup_client_order_id")
                else None
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
