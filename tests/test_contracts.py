import pytest

from trader_api_examples.contracts import ContractError, calculate_amount


def test_calculate_amount_uses_contract_size_and_increment() -> None:
    result = calculate_amount(
        lots=0.02,
        contract_setting={
            "market": "EURUSD",
            "contractSize": 100_000,
            "minTradeLot": 0.01,
            "minLotIncrementUnit": 0.01,
        },
    )

    assert result.amount == 2000
    assert result.minimum_amount == 1000
    assert result.increment_amount == 1000


def test_calculate_amount_rejects_lot_not_on_increment() -> None:
    with pytest.raises(ContractError, match="increment"):
        calculate_amount(
            lots=0.015,
            contract_setting={
                "contractSize": 100_000,
                "minTradeLot": 0.01,
                "minLotIncrementUnit": 0.01,
            },
        )
