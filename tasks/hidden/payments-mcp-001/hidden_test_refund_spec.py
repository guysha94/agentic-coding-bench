"""Hidden tests derived from Confluence page CONF-REFUND-2.

Every assertion here encodes a rule that appears ONLY in the MCP corpus. In particular
rules 4 and 5 contradict what the local charge implementation would suggest, so an agent
that guesses from the codebase instead of retrieving the spec will fail.
"""

from decimal import Decimal

import pytest
from payments.domain.errors import PaymentError
from payments.domain.models import PaymentStatus


def test_refund_error_types_exist():
    from payments.domain.errors import RefundAmountTooLarge, RefundNotAllowed

    assert issubclass(RefundNotAllowed, PaymentError)
    assert issubclass(RefundAmountTooLarge, PaymentError)


def test_full_refund_sets_status_refunded(service, usd):
    payment = service.charge("acc_1", usd("100.00"))
    refunded = service.refund(payment.id)
    assert refunded.status is PaymentStatus.REFUNDED


def test_full_refund_credits_amount_but_not_the_fee(service, accounts, usd):
    """Rule 4: the processing fee is NOT returned. This differs from the charge path."""
    service.charge("acc_1", usd("100.00"))          # debits 103.20
    payment = service.history("acc_1")[-1]
    service.refund(payment.id)                       # credits 100.00 only
    assert accounts.get("acc_1").balance.amount == Decimal("4996.80")


def test_partial_refund_keeps_status_captured(service, usd):
    """Rule 5: after a partial refund further refunds may follow, so status stays CAPTURED."""
    payment = service.charge("acc_1", usd("100.00"))
    refunded = service.refund(payment.id, usd("40.00"))
    assert refunded.status is PaymentStatus.CAPTURED


def test_partial_refund_credits_only_the_refunded_amount(service, accounts, usd):
    service.charge("acc_1", usd("100.00"))           # 5000 - 103.20 = 4896.80
    payment = service.history("acc_1")[-1]
    service.refund(payment.id, usd("40.00"))         # + 40.00
    assert accounts.get("acc_1").balance.amount == Decimal("4936.80")


def test_over_refund_is_rejected(service, usd):
    payment = service.charge("acc_1", usd("50.00"))
    with pytest.raises(PaymentError) as exc:
        service.refund(payment.id, usd("50.01"))
    assert type(exc.value).__name__ == "RefundAmountTooLarge"


def test_refunding_a_non_captured_payment_is_rejected(service, payments, usd):
    from payments.domain.models import Payment

    pending = payments.save(
        Payment(id="pay_pending_x", account_id="acc_1", amount=usd("10.00"),
                status=PaymentStatus.PENDING)
    )
    with pytest.raises(PaymentError) as exc:
        service.refund(pending.id)
    assert type(exc.value).__name__ == "RefundNotAllowed"


def test_double_full_refund_is_rejected(service, usd):
    """Rule 8: after a full refund the status is REFUNDED, so rule 1 blocks a second one."""
    payment = service.charge("acc_1", usd("20.00"))
    service.refund(payment.id)
    with pytest.raises(PaymentError) as exc:
        service.refund(payment.id)
    assert type(exc.value).__name__ == "RefundNotAllowed"


def test_refund_records_audit_entry_with_exact_action(service, audit, usd):
    payment = service.charge("acc_1", usd("30.00"))
    service.refund(payment.id)
    actions = [e.action for e in audit.for_entity(payment.id)]
    assert "payment.refunded" in actions


def test_refund_sends_notification_with_exact_subject(service, notifications, usd):
    payment = service.charge("acc_1", usd("30.00"))
    service.refund(payment.id)
    assert any(n.subject == "Refund issued" for n in notifications.outbox)


def test_refund_unknown_payment_raises_payment_not_found(service):
    from payments.domain.errors import PaymentNotFound

    with pytest.raises(PaymentNotFound):
        service.refund("pay_missing")


def test_api_refund_status_codes(api, service, payments, usd):
    from payments.domain.models import Payment

    captured = service.charge("acc_1", usd("25.00"))
    assert api.refund_payment(captured.id, {}).status == 200
    assert api.refund_payment("pay_nope", {}).status == 404

    pending = payments.save(
        Payment(id="pay_pending_y", account_id="acc_1", amount=usd("10.00"),
                status=PaymentStatus.PENDING)
    )
    assert api.refund_payment(pending.id, {}).status == 409

    another = service.charge("acc_1", usd("25.00"))
    assert api.refund_payment(another.id, {"amount": "999.00"}).status == 400
