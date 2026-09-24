from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PositionUpdateNotification:
    """A position change that may require local ownership reconciliation."""

    event_id: str | None
    affected_refs: frozenset[str]


def _refs_from_records(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    refs: set[str] = set()
    for item in value:
        if isinstance(item, dict):
            for key in ("ref", "orderNo", "dealRef"):
                ref = item.get(key)
                if ref is not None and str(ref):
                    refs.add(str(ref))
                    break
        elif item is not None and str(item):
            refs.add(str(item))
    return refs


def parse_position_update(
    *, event_id: str | None, event_name: str | None, data_lines: list[str]
) -> PositionUpdateNotification | None:
    if not data_lines:
        return None
    try:
        payload: Any = json.loads("\n".join(data_lines))
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None

    nested = payload.get("data")
    if isinstance(nested, dict):
        payload = nested
    fields = {"added", "modified", "deleted", "orderNos", "order_nos"}
    if (
        event_name
        and event_name.lower() not in {"positionupdate", "position_update"}
        and not any(field in payload for field in fields)
    ):
        return None

    refs: set[str] = set()
    for key in ("added", "modified", "deleted", "orderNos", "order_nos"):
        refs.update(_refs_from_records(payload.get(key)))
    if not any(field in payload for field in fields):
        return None
    return PositionUpdateNotification(event_id=event_id, affected_refs=frozenset(refs))


class SsePositionUpdateParser:
    """Parse one SSE response into position update notifications."""

    def __init__(self) -> None:
        self._event_id: str | None = None
        self._last_event_id: str | None = None
        self._event_name: str | None = None
        self._data_lines: list[str] = []

    @property
    def last_event_id(self) -> str | None:
        return self._last_event_id

    def feed_line(self, line: str) -> PositionUpdateNotification | None:
        if line == "":
            notification = parse_position_update(
                event_id=self._event_id,
                event_name=self._event_name,
                data_lines=self._data_lines,
            )
            self._reset()
            return notification
        if line.startswith(":"):
            return None
        field, separator, value = line.partition(":")
        if not separator:
            return None
        value = value[1:] if value.startswith(" ") else value
        if field == "id":
            self._event_id = value
            self._last_event_id = value
        elif field == "event":
            self._event_name = value
        elif field == "data":
            self._data_lines.append(value)
        return None

    def finish(self) -> PositionUpdateNotification | None:
        if not self._data_lines:
            self._reset()
            return None
        notification = parse_position_update(
            event_id=self._event_id,
            event_name=self._event_name,
            data_lines=self._data_lines,
        )
        self._reset()
        return notification

    def _reset(self) -> None:
        self._event_id = None
        self._event_name = None
        self._data_lines = []
