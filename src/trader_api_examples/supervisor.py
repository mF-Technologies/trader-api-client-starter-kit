from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import Protocol


class WorkerProcess(Protocol):
    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


ProcessFactory = Callable[[Sequence[str]], WorkerProcess]


def _start_process(command: Sequence[str]) -> WorkerProcess:
    return subprocess.Popen(command)


def run_supervisor(
    command: Sequence[str],
    *,
    restart_delay_seconds: float,
    process_factory: ProcessFactory = _start_process,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Restart a worker after recoverable runtime failure until explicitly stopped."""
    while True:
        process = process_factory(command)
        try:
            exit_code = process.wait()
        except KeyboardInterrupt:
            print("[Supervisor] Stopping algo worker...", file=sys.stderr, flush=True)
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return 130

        if exit_code != 2:
            return exit_code
        print(
            f"[Supervisor] Worker transport failed; restarting in "
            f"{restart_delay_seconds:g}s...",
            file=sys.stderr,
            flush=True,
        )
        sleep(restart_delay_seconds)
