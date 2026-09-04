import json
from pathlib import Path

from trader_api_examples.runtime import HeartbeatWriter, instance_journal_path


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
