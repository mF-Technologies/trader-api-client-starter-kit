import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from trader_api_examples.supervisor import run_supervisor


class FakeProcess:
    def __init__(self, exit_code: int | BaseException) -> None:
        self.exit_code = exit_code
        self.terminated = False
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        if self.terminated or self.killed:
            return 0
        if isinstance(self.exit_code, BaseException):
            raise self.exit_code
        return self.exit_code

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True


class HungProcess(FakeProcess):
    def wait(self, timeout: float | None = None) -> int:
        if self.killed:
            return 0
        raise subprocess.TimeoutExpired("worker", timeout)


def test_supervisor_restarts_transport_failure_then_returns_worker_result() -> None:
    processes = iter((FakeProcess(2), FakeProcess(0)))
    starts: list[list[str]] = []
    delays: list[float] = []

    def factory(command: Sequence[str], **_: Any) -> FakeProcess:
        starts.append(list(command))
        return next(processes)

    result = run_supervisor(
        ["python", "-m", "worker"],
        restart_delay_seconds=3,
        process_factory=factory,
        sleep=delays.append,
    )

    assert result == 0
    assert len(starts) == 2
    assert delays == [3]


def test_supervisor_restarts_native_worker_crash_then_returns_worker_result(
    capsys: Any,
) -> None:
    processes = iter((FakeProcess(0xC0000005), FakeProcess(0)))
    starts: list[list[str]] = []
    delays: list[float] = []

    def factory(command: Sequence[str], **_: Any) -> FakeProcess:
        starts.append(list(command))
        return next(processes)

    result = run_supervisor(
        ["python", "-m", "worker"],
        restart_delay_seconds=3,
        process_factory=factory,
        sleep=delays.append,
    )

    assert result == 0
    assert len(starts) == 2
    assert delays == [3]
    assert "Worker crashed with exit code 3221225477; restarting in 3s" in capsys.readouterr().err


def test_supervisor_restarts_worker_when_heartbeat_stops(
    tmp_path: Path,
    capsys: Any,
) -> None:
    hung_process = HungProcess(0)
    processes = iter((hung_process, FakeProcess(0)))
    starts: list[list[str]] = []
    delays: list[float] = []
    clock = iter((1_000.0, 1_121.0, 1_122.0))

    def factory(command: Sequence[str], **_: Any) -> FakeProcess:
        starts.append(list(command))
        return next(processes)

    result = run_supervisor(
        ["python", "-m", "worker"],
        restart_delay_seconds=3,
        process_factory=factory,
        sleep=delays.append,
        heartbeat_path=tmp_path / "missing-heartbeat.json",
        heartbeat_timeout_seconds=120,
        monitor_interval_seconds=1,
        clock=lambda: next(clock),
    )

    assert result == 0
    assert hung_process.killed is True
    assert len(starts) == 2
    assert delays == [3]
    assert "Worker heartbeat timed out after 120s; restarting in 3s" in capsys.readouterr().err


def test_supervisor_does_not_restart_blocked_worker() -> None:
    starts = 0

    def factory(command: Sequence[str], **_: Any) -> FakeProcess:
        nonlocal starts
        del command
        starts += 1
        return FakeProcess(4)

    result = run_supervisor(
        ["python", "-m", "worker"],
        restart_delay_seconds=3,
        process_factory=factory,
        sleep=lambda _: None,
    )

    assert result == 4
    assert starts == 1


def test_supervisor_terminates_worker_on_keyboard_interrupt() -> None:
    process = FakeProcess(KeyboardInterrupt())

    result = run_supervisor(
        ["python", "-m", "worker"],
        restart_delay_seconds=3,
        process_factory=lambda *_args, **_kwargs: process,
        sleep=lambda _: None,
    )

    assert result == 130
    assert process.terminated is True
    assert process.killed is False
