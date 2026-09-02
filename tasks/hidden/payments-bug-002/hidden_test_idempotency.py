"""Hidden tests: side effects that must ALSO be suppressed on a replayed charge.

A fix that only returns the existing payment, but still writes an audit entry, sends a
notification, or consumes daily-limit headroom, is incomplete. These tests catch that.
"""

from decimal import Decimal

import pytest
from payments.domain.errors import DailyLimitExceeded


def test_replay_does_not_duplicate_audit_entries(service, audit, usd):
    first = service.charge("acc_1", usd("40.00"), idempotency_key="k1")
    service.charge("acc_1", usd("40.00"), idempotency_key="k1")
    assert len(audit.for_entity(first.id)) == 1


def test_replay_does_not_send_a_second_notification(service, notifications, usd):
    service.charge("acc_1", usd("40.00"), idempotency_key="k1")
    service.charge("acc_1", usd("40.00"), idempotency_key="k1")
    assert len(notifications.outbox) == 1


def test_replay_creates_no_second_payment_record(service, payments, usd):
    service.charge("acc_1", usd("40.00"), idempotency_key="k1")
    service.charge("acc_1", usd("40.00"), idempotency_key="k1")
    assert len(payments.for_account("acc_1")) == 1


def test_many_replays_debit_exactly_once(service, accounts, usd):
    for _ in range(5):
        service.charge("acc_1", usd("100.00"), idempotency_key="stable-key")
    assert accounts.get("acc_1").balance.amount == Decimal("4896.80")


def test_replay_does_not_consume_daily_limit_headroom(service, accounts, usd):
    account = accounts.get("acc_1")
    account.daily_limit = type(account.balance)(Decimal("150.00"), account.balance.currency)
    accounts.save(account)

    for _ in range(3):
        service.charge("acc_1", usd("100.00"), idempotency_key="same")

    # The replays must not count toward the limit, so a genuinely new 40.00 charge fits.
    service.charge("acc_1", usd("40.00"), idempotency_key="different")

    with pytest.raises(DailyLimitExceeded):
        service.charge("acc_1", usd("50.00"), idempotency_key="third")


def test_distinct_keys_still_charge_separately(service, accounts, usd):
    service.charge("acc_1", usd("10.00"), idempotency_key="a")
    service.charge("acc_1", usd("10.00"), idempotency_key="b")
    # Two charges of 10.00 + 0.59 fee each.
    assert accounts.get("acc_1").balance.amount == Decimal("4978.82")


def test_unkeyed_charges_are_never_deduplicated(service, payments, usd):
    service.charge("acc_1", usd("5.00"))
    service.charge("acc_1", usd("5.00"))
    assert len(payments.for_account("acc_1")) == 2
