from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from .algo import Outcome
from .api import ApiError
from .commands import (
    account_inspector,
    algo_runner,
    contract_calculator,
    live_algo,
    market_data_monitor,
    order_lifecycle,
    recover,
    replay_algo,
)
from .config import ConfigError, load_config
from .contracts import ContractError
from .output import CommandResult, write_result
from .safety import LiveExecutionBlocked
from .supervisor import run_supervisor

COMMANDS = (
    "account-inspector",
    "contract-calculator",
    "market-data-monitor",
    "order-lifecycle-checker",
    "rsi-algo-demo",
    "algo-runner",
    "recover",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trader-api-examples")
    parser.add_argument("--output", choices=("human", "json"), default="human")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in COMMANDS:
        child = subparsers.add_parser(command)
        child.add_argument("--config", type=Path, required=True)
        child.add_argument("--output", choices=("human", "json"), default=argparse.SUPPRESS)
        if command == "contract-calculator":
            child.add_argument("--lots", type=float, default=0.01)
        if command in {"order-lifecycle-checker", "recover"}:
            child.add_argument("--execute", action="store_true")
        if command == "rsi-algo-demo":
            child.add_argument(
                "--mode", choices=("replay", "live-observe", "live-execute"), default="replay"
            )
            child.add_argument("--execute", action="store_true")
            child.add_argument("--fixture", type=Path)
        if command == "algo-runner":
            child.add_argument(
                "--mode", choices=("live-observe", "live-execute"), default="live-observe"
            )
            child.add_argument("--execute", action="store_true")
            child.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
        if command == "market-data-monitor":
            child.add_argument("--bars-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = getattr(args, "output", "human")
    replay = args.command == "rsi-algo-demo" and args.mode == "replay"
    try:
        config = load_config(args.config, require_api_key=not replay)
        if args.command == "algo-runner" and not args.worker:
            return run_supervisor(
                _algo_worker_command(args, output),
                restart_delay_seconds=config.trading.market_data_retry_seconds,
            )
        result = _run_command(args, config)
    except ApiError as error:
        outcome = Outcome.ERROR if error.is_transient_response else Outcome.BLOCKED
        result = CommandResult(args.command, outcome, str(error), {})
    except (ConfigError, ContractError, LiveExecutionBlocked, ValueError) as error:
        result = CommandResult(args.command, Outcome.BLOCKED, str(error), {})
    except (RuntimeError, TimeoutError) as error:
        result = CommandResult(args.command, Outcome.ERROR, str(error), {})
    except Exception as error:
        print(f"Unexpected {type(error).__name__}; details were withheld.", file=sys.stderr)
        result = CommandResult(args.command, Outcome.ERROR, "Unexpected command failure.", {})
    write_result(result, output=output, stream=sys.stdout)
    return _exit_code(result.outcome)


def _algo_worker_command(args: argparse.Namespace, output: str) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "trader_api_examples.cli",
        "algo-runner",
        "--config",
        str(args.config),
        "--mode",
        str(args.mode),
        "--output",
        output,
        "--worker",
    ]
    if args.execute:
        command.append("--execute")
    return command


def _run_command(args: argparse.Namespace, config: object) -> CommandResult:
    from .config import AppConfig

    if not isinstance(config, AppConfig):
        raise TypeError("Invalid application configuration.")
    if args.command == "account-inspector":
        return asyncio.run(account_inspector(config))
    if args.command == "contract-calculator":
        return asyncio.run(contract_calculator(config, args.lots))
    if args.command == "market-data-monitor":
        return asyncio.run(market_data_monitor(config, with_quote=not args.bars_only))
    if args.command == "order-lifecycle-checker":
        return asyncio.run(order_lifecycle(config, args.execute))
    if args.command == "recover":
        return asyncio.run(recover(config, args.execute))
    if args.command == "rsi-algo-demo":
        if args.mode == "replay":
            return replay_algo(config, args.fixture)
        return asyncio.run(live_algo(config, args.mode, args.execute))
    if args.command == "algo-runner":
        return asyncio.run(algo_runner(config, args.mode, args.execute))
    raise ValueError(f"Unknown command: {args.command}")


def _exit_code(outcome: Outcome) -> int:
    return {
        Outcome.SUCCESS: 0,
        Outcome.NO_SIGNAL: 0,
        Outcome.INCONCLUSIVE: 3,
        Outcome.BLOCKED: 4,
        Outcome.ERROR: 2,
    }[outcome]


if __name__ == "__main__":
    raise SystemExit(main())
