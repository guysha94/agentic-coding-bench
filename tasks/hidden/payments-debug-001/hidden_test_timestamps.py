"""Hidden tests: the fix must be correct, not merely make the crash go away.

Stripping tzinfo from live payments, or sorting on something other than created_at, both
silence the TypeError while corrupting the data. These tests reject those shortcuts.
"""

from datetime import UTC, datetime

from payments.services.legacy_import import import_legacy_payments


def test_legacy_payments_are_timezone_aware(payments):
    imported = import_legacy_payments(payments)
    assert imported, "legacy import returned nothing"
    for payment in imported:
        assert payment.created_at.tzinfo is not None, (
            "legacy payments must be timezone-aware after import"
        )


def test_legacy_wall_clock_instant_is_preserved(payments):
    imported = import_legacy_payments(payments)
    first = min(imported, key=lambda p: p.created_at)
    # 2023-11-02T09:15:00 interpreted as UTC.
    assert first.created_at.astimezone(UTC).replace(tzinfo=None) == datetime(
        2023, 11, 2, 9, 15, 0
    )


def test_live_payments_remain_timezone_aware(service, usd):
    payment = service.charge("acc_1", usd("10.00"))
    assert payment.created_at.tzinfo is not None, (
        "live payments must keep their timezone; stripping tzinfo is not a valid fix"
    )


def test_history_is_ordered_oldest_first(service, payments, usd):
    import_legacy_payments(payments)
    service.charge("acc_1", usd("10.00"))
    history = service.history("acc_1")
    assert len(history) == 3
    timestamps = [p.created_at for p in history]
    assert timestamps == sorted(timestamps)
    assert history[0].metadata.get("source") == "legacy_ledger"
    assert history[-1].metadata.get("source") is None


def test_history_still_works_without_legacy_rows(service, usd):
    service.charge("acc_1", usd("5.00"))
    assert len(service.history("acc_1")) == 1


def test_no_legacy_rows_were_dropped(payments):
    imported = import_legacy_payments(payments)
    assert len(imported) == 2
    assert len(payments.for_account("acc_1")) == 2
