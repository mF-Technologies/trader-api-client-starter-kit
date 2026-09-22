from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol


class WorkerProcess(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


ProcessFactory = Callable[[Sequence[str]], WorkerProcess]

TERMINAL_EXIT_CODES = {
    0,
    3,
    4,
    130,
    -2,
    -15,
    0xC000013A,
    -1073741510,
}


def _start_process(command: Sequence[str]) -> WorkerProcess:
    return subprocess.Popen(command)


def _last_heartbeat_time(path: Path, *, started_at: float) -> float:
    try:
        return max(started_at, path.stat().st_mtime)
    except OSError:
        return started_at


def run_supervisor(
    command: Sequence[str],
    *,
    restart_delay_seconds: float,
    process_factory: ProcessFactory = _start_process,
    sleep: Callable[[float], None] = time.sleep,
    heartbeat_path: Path = Path("runtime/algo-heartbeat.json"),
    heartbeat_timeout_seconds: float = 120,
    monitor_interval_seconds: float = 1,
    clock: Callable[[], float] = time.time,
) -> int:
    """Restart a worker after recoverable runtime failure until explicitly stopped."""
    while True:
        process = process_factory(command)
        started_at = clock()
        failure: str | None = None
        try:
            while True:
                try:
                    exit_code = process.wait(timeout=monitor_interval_seconds)
                    break
                except subprocess.TimeoutExpired:
                    last_heartbeat = _last_heartbeat_time(
                        heartbeat_path,
                        started_at=started_at,
                    )
                    if clock() - last_heartbeat <= heartbeat_timeout_seconds:
                        continue
                    process.kill()
                    process.wait()
                    exit_code = 2
                    failure = f"heartbeat timed out after {heartbeat_timeout_seconds:g}s"
                    break
        except KeyboardInterrupt:
            print("[Supervisor] Stopping algo worker...", file=sys.stderr, flush=True)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return 130

        if exit_code in TERMINAL_EXIT_CODES:
            return exit_code

        if failure is None:
            failure = (
                "transport failed" if exit_code == 2 else f"crashed with exit code {exit_code}"
            )
        print(
            f"[Supervisor] Worker {failure}; restarting in {restart_delay_seconds:g}s...",
            file=sys.stderr,
            flush=True,
        )
        sleep(restart_delay_seconds)
