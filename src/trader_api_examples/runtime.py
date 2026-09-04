from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

SENSITIVE_EVENT_KEYS = {
    "access_token",
    "api_key",
    "password",
    "price_tag",
    "token",
    "trade_key",
    "username",
    "valid_generated_token",
}


def _safe_name(value: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in normalized.split("-") if part)


def instance_journal_path(instance_name: str, contract: str) -> Path:
    return Path("runtime") / f"execution-{_safe_name(instance_name)}-{_safe_name(contract)}.json"


def _reject_sensitive_event_fields(value: Any, path: str = "event") -> None:
    if isinstance(value, dict):
        for raw_key, nested in value.items():
            key = str(raw_key).lower()
            if key in SENSITIVE_EVENT_KEYS or key.endswith("_token") or key.endswith("_password"):
                raise ValueError(f"Sensitive event field is not allowed: {path}.{raw_key}")
            _reject_sensitive_event_fields(nested, f"{path}.{raw_key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _reject_sensitive_event_fields(nested, f"{path}[{index}]")


class InstanceEventLog:
    def __init__(
        self,
        instance_name: str,
        *,
        directory: Path = Path("runtime/logs"),
        max_bytes: int = 10 * 1024 * 1024,
        backup_count: int = 5,
    ) -> None:
        self.instance_name = instance_name
        self.path = directory / f"{_safe_name(instance_name)}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handler = RotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))

    def write(self, event: str, **fields: Any) -> None:
        _reject_sensitive_event_fields(fields)
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "instance": self.instance_name,
            "event": event,
            **fields,
        }
        record = logging.LogRecord(
            name=f"trader_api_examples.instance.{_safe_name(self.instance_name)}",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg=json.dumps(payload, separators=(",", ":")),
            args=(),
            exc_info=None,
        )
        self._handler.emit(record)
        self._handler.flush()

    def close(self) -> None:
        self._handler.close()


class HeartbeatWriter:
    def __init__(self, path: Path = Path("runtime/algo-heartbeat.json")) -> None:
        self.path = path

    def write(self, instances: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now(UTC).isoformat(),
            "instances": instances,
        }
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(temporary, self.path)
