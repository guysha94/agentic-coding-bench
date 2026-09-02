"""Hidden tests for the cancellation feature.

These pin the parts of the specification that are easy to implement *almost* right:
exact status codes, the audit action string, the notification subject, and the metadata
key. They also confirm the change respects the existing layering.
"""

from decimal import Decimal

import pytest
from payments.domain.models import Payment, PaymentStatus


def _pending(service, payments, usd) -> Payment:
    """A pending payment, created directly so the charge path's auto-capture is bypassed."""
    payment = Payment(
        id="pay_pending_1",
        account_id="acc_1",
        amount=usd("25.00"),
        status=PaymentStatus.PENDING,
    )
    return payments.save(payment)


def test_cancelled_status_exists():
    assert hasattr(PaymentStatus, "CANCELLED")
    assert PaymentStatus.CANCELLED.value == "cancelled"


def test_cancel_sets_status_and_reason(service, payments, usd):
    payment = _pending(service, payments, usd)
    result = service.cancel(payment.id, "customer request")
    assert result.status is PaymentStatus.CANCELLED
    assert result.metadata["cancellation_reason"] == "customer request"


def test_cancel_records_audit_entry(service, payments, audit, usd):
    payment = _pending(service, payments, usd)
    service.cancel(payment.id, "fraud review")
    assert "payment.cancelled" in [e.action for e in audit.for_entity(payment.id)]


def test_cancel_sends_notification_with_exact_subject(service, payments, notifications, usd):
    payment = _pending(service, payments, usd)
    service.cancel(payment.id, "duplicate")
    assert any(n.subject == "Payment cancelled" for n in notifications.outbox)
    assert any(n.recipient == "alice@example.com" for n in notifications.outbox)


def test_cancel_rejects_non_pending_payment(service, usd):
    from payments.domain.errors import PaymentError

    captured = service.charge("acc_1", usd("10.00"))
    with pytest.raises(PaymentError) as exc:
        service.cancel(captured.id, "too late")
    assert type(exc.value).__name__ == "CancellationNotAllowed"


def test_cancellation_error_derives_from_payment_error():
    from payments.domain.errors import CancellationNotAllowed, PaymentError

    assert issubclass(CancellationNotAllowed, PaymentError)


def test_cancel_rejects_empty_reason(service, payments, usd):
    payment = _pending(service, payments, usd)
    with pytest.raises(ValueError):
        service.cancel(payment.id, "")


def test_cancel_unknown_payment_raises(service):
    from payments.domain.errors import PaymentNotFound

    with pytest.raises(PaymentNotFound):
        service.cancel("pay_missing", "whatever")


def test_api_cancel_returns_200(api, payments, usd):
    payments.save(
        Payment(id="pay_api_1", account_id="acc_1", amount=usd("15.00"),
                status=PaymentStatus.PENDING)
    )
    response = api.cancel_payment("pay_api_1", {"reason": "customer request"})
    assert response.status == 200
    assert response.body["status"] == "cancelled"


def test_api_cancel_conflict_is_409(api, service, usd):
    captured = service.charge("acc_1", usd("10.00"))
    assert api.cancel_payment(captured.id, {"reason": "late"}).status == 409


def test_api_cancel_unknown_payment_is_404(api):
    assert api.cancel_payment("pay_nope", {"reason": "x"}).status == 404


def test_api_cancel_missing_reason_is_400(api, payments, usd):
    payments.save(
        Payment(id="pay_api_2", account_id="acc_1", amount=usd("15.00"),
                status=PaymentStatus.PENDING)
    )
    assert api.cancel_payment("pay_api_2", {}).status == 400
    assert api.cancel_payment("pay_api_2", {"reason": "  "}).status == 400


def test_cancelling_does_not_move_money(service, payments, accounts, usd):
    before = accounts.get("acc_1").balance.amount
    payment = _pending(service, payments, usd)
    service.cancel(payment.id, "no charge yet")
    assert accounts.get("acc_1").balance.amount == before
    assert before == Decimal("5000.00")
