"""Hidden tests: additional rounding boundaries the agent never saw.

These exist so a fix that special-cases the visible test values cannot pass.
"""

from decimal import ROUND_HALF_UP, Decimal

from payments.domain.fees import calculate_fee, calculate_net
from payments.domain.models import Currency, Money

PERCENT = Decimal("0.029")
FIXED = Decimal("0.30")


def expected(amount: str) -> Decimal:
    raw = Decimal(amount) * PERCENT + FIXED
    return raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def test_rounding_across_many_amounts():
    for amount in ["0.01", "1.00", "7.77", "12.34", "19.99", "33.33", "99.99",
                   "150.55", "999.99", "4321.09"]:
        got = calculate_fee(Money(Decimal(amount))).amount
        assert got == expected(amount), f"fee for {amount}: got {got}, want {expected(amount)}"


def test_half_cent_rounds_up_not_down():
    # Raw fee lands exactly on a half cent; half-up must round away from zero.
    assert calculate_fee(Money(Decimal("6.20"))).amount == Decimal("0.48")


def test_net_still_consistent_with_fee():
    amount = Money(Decimal("250.00"))
    assert calculate_net(amount).amount == amount.amount - calculate_fee(amount).amount


def test_currency_is_preserved():
    for currency in (Currency.USD, Currency.EUR, Currency.GBP):
        assert calculate_fee(Money(Decimal("10.00"), currency)).currency is currency
