import json
from pathlib import Path

import pytest

from trader_api_examples.runtime import (
    HeartbeatWriter,
    InstanceEventLog,
    instance_journal_path,
)


def test_instance_journal_paths_are_stable_and_separate() -> None:
    assert instance_journal_path("Gold RSI", "LLG") == Path("runtime/execution-gold-rsi-llg.json")
    assert instance_journal_path("Euro EMA", "EUR/USD") == Path(
        "runtime/execution-euro-ema-eur-usd.json"
    )


def test_heartbeat_writer_replaces_file_with_non_sensitive_status(tmp_path: Path) -> None:
    path = tmp_path / "heartbeat.json"
    writer = HeartbeatWriter(path)

    writer.write({"gold-rsi": "streaming"})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["instances"] == {"gold-rsi": "streaming"}
    assert payload["updated_at"].endswith("+00:00")
    assert not path.with_suffix(".json.tmp").exists()


def test_instance_event_log_writes_structured_json_to_separate_files(tmp_path: Path) -> None:
    rsi = InstanceEventLog("rsi-llg", directory=tmp_path)
    macd = InstanceEventLog("macd-llg", directory=tmp_path)

    rsi.write("BAR_EVALUATED", close=4000.5, indicators={"rsi": 51.2})
    macd.write("SIGNAL", signal="OPEN_BUY", indicators={"histogram": 0.25})
    rsi.close()
    macd.close()

    rsi_event = json.loads((tmp_path / "rsi-llg.jsonl").read_text(encoding="utf-8"))
    macd_event = json.loads((tmp_path / "macd-llg.jsonl").read_text(encoding="utf-8"))
    assert rsi_event["instance"] == "rsi-llg"
    assert rsi_event["event"] == "BAR_EVALUATED"
    assert rsi_event["indicators"] == {"rsi": 51.2}
    assert macd_event["signal"] == "OPEN_BUY"


def test_instance_event_log_rejects_sensitive_fields(tmp_path: Path) -> None:
    event_log = InstanceEventLog("rsi", directory=tmp_path)

    with pytest.raises(ValueError, match="api_key"):
        event_log.write(
            "CONNECTED",
            api_key="must-not-be-written",  # pragma: allowlist secret
        )

    event_log.close()
    assert not (tmp_path / "rsi.jsonl").exists()


def test_instance_event_log_rotates_at_configured_size(tmp_path: Path) -> None:
    event_log = InstanceEventLog("rsi", directory=tmp_path, max_bytes=180, backup_count=2)

    for index in range(10):
        event_log.write("BAR_EVALUATED", index=index, detail="x" * 40)
    event_log.close()

    assert (tmp_path / "rsi.jsonl").exists()
    assert (tmp_path / "rsi.jsonl.1").exists()
