from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any


class ContractError(ValueError):
    """Raised when an amount is invalid for a contract."""


@dataclass(frozen=True)
class AmountCalculation:
    lots: float
    amount: float
    minimum_amount: float
    increment_amount: float


def _decimal(value: Any, name: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ContractError(f"Contract setting {name} is missing or invalid.") from error


def calculate_amount(*, lots: float, contract_setting: dict[str, Any]) -> AmountCalculation:
    lots_value = _decimal(lots, "lots")
    contract_size = _decimal(contract_setting.get("contractSize"), "contractSize")
    minimum_lots = _decimal(contract_setting.get("minTradeLot"), "minTradeLot")
    increment_lots = _decimal(contract_setting.get("minLotIncrementUnit"), "minLotIncrementUnit")
    if lots_value < minimum_lots:
        raise ContractError(f"lots must be at least {minimum_lots}.")
    if increment_lots <= 0 or (lots_value - minimum_lots) % increment_lots != 0:
        raise ContractError(f"lots must follow the {increment_lots} lot increment.")
    return AmountCalculation(
        lots=float(lots_value),
        amount=float(lots_value * contract_size),
        minimum_amount=float(minimum_lots * contract_size),
        increment_amount=float(increment_lots * contract_size),
    )


def find_contract_setting(settings: list[dict[str, Any]], contract: str) -> dict[str, Any]:
    wanted = contract.upper()
    for setting in settings:
        code = setting.get("market") or setting.get("contractCode") or setting.get("contract")
        if str(code).upper() == wanted:
            return setting
    raise ContractError(f"Contract {contract} is not available for this account.")


def validate_amount(*, amount: float, contract_setting: dict[str, Any]) -> AmountCalculation:
    contract_size = _decimal(contract_setting.get("contractSize"), "contractSize")
    if contract_size <= 0:
        raise ContractError("contractSize must be greater than zero.")
    return calculate_amount(
        lots=float(_decimal(amount, "amount") / contract_size), contract_setting=contract_setting
    )
