"""Hidden tests for per-currency daily limits.

The failure mode these target is a *partial* migration: the model changes but some caller,
seed value or fallback is left on the old single-limit behaviour.
"""

from decimal import Decimal

import pytest
from payments.domain.errors import DailyLimitExceeded
from payments.domain.models import Account, Currency, Money


def test_account_exposes_per_currency_limits(accounts):
    account = accounts.get("acc_1")
    assert hasattr(account, "daily_limits"), "Account should expose `daily_limits`"
    assert isinstance(account.daily_limits, dict)
    assert account.daily_limits[Currency.USD].amount == Decimal("10000.00")
    assert account.daily_limits[Currency.EUR].amount == Decimal("2000.00")


def test_old_single_limit_attribute_is_gone(accounts):
    assert not hasattr(accounts.get("acc_1"), "daily_limit"), (
        "`daily_limit` should have been replaced by `daily_limits`, not kept alongside it"
    )


def test_defaults_are_defined_for_every_currency():
    from payments.repositories.memory import DEFAULT_DAILY_LIMITS

    for currency in (Currency.USD, Currency.EUR, Currency.GBP):
        assert DEFAULT_DAILY_LIMITS[currency].amount == Decimal("10000.00")


def test_limit_is_enforced_in_the_payments_own_currency(accounts, service):
    account = accounts.get("acc_1")
    account.balance = Money(Decimal("100000.00"), Currency.USD)
    accounts.save(account)

    # EUR limit is 2000.
    service.charge("acc_1", Money(Decimal("1500.00"), Currency.EUR))
    with pytest.raises(DailyLimitExceeded):
        service.charge("acc_1", Money(Decimal("600.00"), Currency.EUR))


def test_one_currency_does_not_consume_another_currencys_headroom(accounts, service):
    account = accounts.get("acc_1")
    account.balance = Money(Decimal("100000.00"), Currency.USD)
    accounts.save(account)

    # Spend the EUR limit entirely.
    service.charge("acc_1", Money(Decimal("2000.00"), Currency.EUR))
    # A USD payment must still be allowed: its own 10000 limit is untouched.
    payment = service.charge("acc_1", Money(Decimal("5000.00"), Currency.USD))
    assert payment.amount.currency is Currency.USD


def test_unconfigured_currency_falls_back_to_the_default(accounts, service):
    account = Account(
        id="acc_gbp",
        email="dave@example.com",
        balance=Money(Decimal("100000.00"), Currency.USD),
        daily_limits={Currency.USD: Money(Decimal("10000.00"), Currency.USD)},
    )
    accounts.save(account)

    # No GBP limit configured -> falls back to the 10000 default, so this is allowed.
    service.charge("acc_gbp", Money(Decimal("9000.00"), Currency.GBP))
    with pytest.raises(DailyLimitExceeded):
        service.charge("acc_gbp", Money(Decimal("1500.00"), Currency.GBP))


def test_usd_limit_still_enforced(accounts, service):
    account = accounts.get("acc_1")
    account.balance = Money(Decimal("100000.00"), Currency.USD)
    account.daily_limits[Currency.USD] = Money(Decimal("500.00"), Currency.USD)
    accounts.save(account)

    service.charge("acc_1", Money(Decimal("400.00"), Currency.USD))
    with pytest.raises(DailyLimitExceeded):
        service.charge("acc_1", Money(Decimal("200.00"), Currency.USD))
