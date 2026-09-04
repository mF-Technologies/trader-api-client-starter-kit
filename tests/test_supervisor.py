from collections.abc import Sequence
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
