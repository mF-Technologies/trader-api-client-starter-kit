from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


def _safe_name(value: str) -> str:
    normalized = "".join(character.lower() if character.isalnum() else "-" for character in value)
    return "-".join(part for part in normalized.split("-") if part)


def instance_journal_path(instance_name: str, contract: str) -> Path:
    return Path("runtime") / f"execution-{_safe_name(instance_name)}-{_safe_name(contract)}.json"


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
