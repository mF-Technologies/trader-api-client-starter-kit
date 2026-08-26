from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, TextIO

from .algo import Outcome


@dataclass(frozen=True)
class CommandResult:
    command: str
    outcome: Outcome
    message: str
    data: dict[str, Any]
    schema_version: str = "1"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value


def write_result(result: CommandResult, *, output: str, stream: TextIO) -> None:
    if output == "json":
        stream.write(json.dumps(_jsonable(asdict(result)), separators=(",", ":")) + "\n")
        return
    stream.write(f"[{result.outcome.value}] {result.message}\n")
    for key, value in result.data.items():
        stream.write(f"{key}: {value}\n")
