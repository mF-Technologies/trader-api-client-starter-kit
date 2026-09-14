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
    contract_calculator,
    execution_cost_audit,
    export_sma_bars,
    live_algo,
    live_ema_algo,
    live_sma_algo,
    market_data_monitor,
    order_lifecycle,
    recover,
    replay_algo,
    replay_ema_algo,
    replay_sma_algo,
)
from .config import ConfigError, load_config
from .contracts import ContractError
from .output import CommandResult, write_result
from .safety import LiveExecutionBlocked

COMMANDS = (
    "account-inspector",
    "contract-calculator",
    "execution-cost-audit",
    "sma-data-export",
    "market-data-monitor",
    "order-lifecycle-checker",
    "rsi-algo-demo",
    "sma-algo-demo",
    "ema-algo-demo",
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
        if command == "execution-cost-audit":
            child.add_argument(
                "--mode",
                choices=("snapshot", "close-existing", "round-trip"),
                default="snapshot",
            )
            child.add_argument("--execute", action="store_true")
            child.add_argument("--amount", type=float)
            child.add_argument("--hold-seconds", type=float, default=2.0)
            child.add_argument("--samples", type=int, default=1)
            child.add_argument("--interval-seconds", type=float, default=1.0)
            child.add_argument("--order-ref")
        if command == "sma-data-export":
            child.add_argument("--output-file", type=Path, required=True)
            child.add_argument("--count", type=int)
        if command in {"order-lifecycle-checker", "recover"}:
            child.add_argument("--execute", action="store_true")
        if command == "rsi-algo-demo":
            child.add_argument(
                "--mode", choices=("replay", "live-observe", "live-execute"), default="replay"
            )
            child.add_argument("--execute", action="store_true")
            child.add_argument("--fixture", type=Path)
        if command == "sma-algo-demo":
            child.add_argument(
                "--mode", choices=("replay", "live-observe", "live-execute"), default="replay"
            )
            child.add_argument("--execute", action="store_true")
            child.add_argument("--fixture", type=Path)
            child.add_argument("--buffer", type=float)
            child.add_argument("--stop-multiple", type=float)
            child.add_argument("--amount", type=float)
            child.add_argument("--annualization-factor", type=float)
            child.add_argument("--commission-rate", type=float)
            child.add_argument("--slippage-bps", type=float)
            child.add_argument("--commission-per-unit", type=float)
            child.add_argument("--commission-round-turn-per-lot", type=float)
            child.add_argument("--amount-per-lot", type=float)
            child.add_argument("--spread-bps", type=float)
            child.add_argument("--financing-bps-per-day", type=float)
            child.add_argument("--market-impact-bps", type=float)
            child.add_argument("--max-holding-hours", type=float)
            child.add_argument("--max-trade-loss-pct", type=float)
            child.add_argument("--max-daily-loss-pct", type=float)
            child.add_argument("--max-drawdown-pct", type=float)
            child.add_argument("--evaluation-start-index", type=int, default=0)
            child.add_argument("--evaluation-end-index", type=int)
        if command == "ema-algo-demo":
            child.add_argument(
                "--mode", choices=("replay", "live-observe", "live-execute"), default="replay"
            )
            child.add_argument("--execute", action="store_true")
            child.add_argument("--fixture", type=Path)
            child.add_argument("--buffer", type=float)
            child.add_argument("--stop-multiple", type=float)
            child.add_argument("--amount", type=float)
            child.add_argument("--annualization-factor", type=float)
            child.add_argument("--commission-rate", type=float)
            child.add_argument("--slippage-bps", type=float)
            child.add_argument("--commission-per-unit", type=float)
            child.add_argument("--commission-round-turn-per-lot", type=float)
            child.add_argument("--amount-per-lot", type=float)
            child.add_argument("--spread-bps", type=float)
            child.add_argument("--financing-bps-per-day", type=float)
            child.add_argument("--market-impact-bps", type=float)
            child.add_argument("--max-holding-hours", type=float)
            child.add_argument("--max-trade-loss-pct", type=float)
            child.add_argument("--max-daily-loss-pct", type=float)
            child.add_argument("--max-drawdown-pct", type=float)
            child.add_argument("--evaluation-start-index", type=int, default=0)
            child.add_argument("--evaluation-end-index", type=int)
        if command == "market-data-monitor":
            child.add_argument("--bars-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    output = getattr(args, "output", "human")
    replay = (
        args.command in {"rsi-algo-demo", "sma-algo-demo", "ema-algo-demo"}
        and args.mode == "replay"
    )
    try:
        config = load_config(args.config, require_api_key=not replay)
        result = _run_command(args, config)
    except (ApiError, ConfigError, ContractError, LiveExecutionBlocked, ValueError) as error:
        result = CommandResult(args.command, Outcome.BLOCKED, str(error), {})
    except (RuntimeError, TimeoutError) as error:
        result = CommandResult(args.command, Outcome.ERROR, str(error), {})
    except Exception as error:
        print(f"Unexpected {type(error).__name__}; details were withheld.", file=sys.stderr)
        result = CommandResult(args.command, Outcome.ERROR, "Unexpected command failure.", {})
    write_result(result, output=output, stream=sys.stdout)
    return _exit_code(result.outcome)


def _run_command(args: argparse.Namespace, config: object) -> CommandResult:
    from .config import AppConfig

    if not isinstance(config, AppConfig):
        raise TypeError("Invalid application configuration.")
    if args.command == "account-inspector":
        return asyncio.run(account_inspector(config))
    if args.command == "contract-calculator":
        return asyncio.run(contract_calculator(config, args.lots))
    if args.command == "execution-cost-audit":
        return asyncio.run(
            execution_cost_audit(
                config,
                mode=args.mode,
                execute=args.execute,
                amount=args.amount,
                hold_seconds=args.hold_seconds,
                samples=args.samples,
                interval_seconds=args.interval_seconds,
                order_ref=args.order_ref,
            )
        )
    if args.command == "sma-data-export":
        return asyncio.run(export_sma_bars(config, args.output_file, args.count))
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
    if args.command == "sma-algo-demo":
        if args.mode == "replay":
            return replay_sma_algo(
                config,
                args.fixture,
                buffer_override=args.buffer,
                stop_override=args.stop_multiple,
                amount_override=args.amount,
                annualization_factor_override=args.annualization_factor,
                commission_rate_override=args.commission_rate,
                slippage_bps_override=args.slippage_bps,
                commission_per_unit_override=args.commission_per_unit,
                commission_round_turn_per_lot_override=args.commission_round_turn_per_lot,
                amount_per_lot_override=args.amount_per_lot,
                spread_bps_override=args.spread_bps,
                financing_bps_per_day_override=args.financing_bps_per_day,
                market_impact_bps_override=args.market_impact_bps,
                max_holding_hours=args.max_holding_hours,
                max_trade_loss_pct=args.max_trade_loss_pct,
                max_daily_loss_pct=args.max_daily_loss_pct,
                max_drawdown_pct=args.max_drawdown_pct,
                evaluation_start_index=args.evaluation_start_index,
                evaluation_end_index=args.evaluation_end_index,
            )
        return asyncio.run(live_sma_algo(config, args.mode, args.execute))
    if args.command == "ema-algo-demo":
        if args.mode == "replay":
            return replay_ema_algo(
                config,
                args.fixture,
                buffer_override=args.buffer,
                stop_override=args.stop_multiple,
                amount_override=args.amount,
                annualization_factor_override=args.annualization_factor,
                commission_rate_override=args.commission_rate,
                slippage_bps_override=args.slippage_bps,
                commission_per_unit_override=args.commission_per_unit,
                commission_round_turn_per_lot_override=args.commission_round_turn_per_lot,
                amount_per_lot_override=args.amount_per_lot,
                spread_bps_override=args.spread_bps,
                financing_bps_per_day_override=args.financing_bps_per_day,
                market_impact_bps_override=args.market_impact_bps,
                max_holding_hours=args.max_holding_hours,
                max_trade_loss_pct=args.max_trade_loss_pct,
                max_daily_loss_pct=args.max_daily_loss_pct,
                max_drawdown_pct=args.max_drawdown_pct,
                evaluation_start_index=args.evaluation_start_index,
                evaluation_end_index=args.evaluation_end_index,
            )
        return asyncio.run(live_ema_algo(config, args.mode, args.execute))
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
