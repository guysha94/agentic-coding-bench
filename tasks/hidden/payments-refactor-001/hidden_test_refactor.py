"""Hidden tests for the refactor.

A refactor is only real if the seam is genuinely injectable and behaviour is unchanged.
These check both, plus that the message text actually moved out of the service.
"""

import inspect

from payments.services import payment_service as payment_service_module
from payments.services.payment_service import PaymentService


def test_templates_module_exists():
    from payments.services.templates import NotificationTemplates

    assert hasattr(NotificationTemplates, "payment_captured")


def test_behaviour_is_preserved(service, notifications, usd):
    service.charge("acc_1", usd("100.00"))
    assert len(notifications.outbox) == 1
    note = notifications.outbox[0]
    assert note.recipient == "alice@example.com"
    assert note.subject == "Payment received"
    assert "100.00" in note.body and "USD" in note.body


def test_templates_are_injectable(accounts, payments, audit, notifications, usd):
    class LoudTemplates:
        def payment_captured(self, payment, account):
            return ("CUSTOM SUBJECT", f"CUSTOM BODY {payment.amount.amount}")

    service = PaymentService(accounts, payments, audit, notifications)
    # The seam must be reachable by keyword or positional injection.
    try:
        service = PaymentService(
            accounts, payments, audit, notifications, templates=LoudTemplates()
        )
    except TypeError:
        service = PaymentService(accounts, payments, audit, notifications, LoudTemplates())

    service.charge("acc_1", usd("42.00"))
    assert notifications.outbox[-1].subject == "CUSTOM SUBJECT"
    assert "CUSTOM BODY" in notifications.outbox[-1].body


def test_service_no_longer_builds_message_text():
    source = inspect.getsource(payment_service_module)
    assert "Payment received" not in source, (
        "notification subject text is still inline in payment_service.py; "
        "it should live in the templates module"
    )


def test_default_templates_used_when_not_injected(accounts, payments, usd):
    from payments.services.notifications import NotificationService

    notifications = NotificationService()
    service = PaymentService(accounts, payments, notifications=notifications)
    service.charge("acc_1", usd("10.00"))
    assert notifications.outbox[0].subject == "Payment received"
