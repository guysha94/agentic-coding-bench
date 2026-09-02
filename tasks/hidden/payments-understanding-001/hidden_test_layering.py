"""Hidden tests: the rule must live in the right layer, not merely produce the right HTTP code.

The decisive test calls the SERVICE directly. An implementation that only checks in the
API layer passes the visible tests and fails here.
"""

import pathlib
from decimal import Decimal

import pytest
import payments
from payments.domain.errors import PaymentError


def test_approval_error_exists_in_domain_layer():
    from payments.domain.errors import ApprovalRequired

    assert issubclass(ApprovalRequired, PaymentError)


def test_service_blocks_large_unapproved_payment(service, usd):
    with pytest.raises(PaymentError) as exc:
        service.charge("acc_1", usd("3000.00"))
    assert type(exc.value).__name__ == "ApprovalRequired"


def test_service_allows_large_approved_payment(service, usd):
    payment = service.charge(
        "acc_1", usd("3000.00"), metadata={"approved_by": "risk-team"}
    )
    assert payment.amount.amount == Decimal("3000.00")


def test_threshold_is_inclusive(service, usd):
    with pytest.raises(PaymentError):
        service.charge("acc_1", usd("3000.00"))
    # Just below the threshold is unaffected.
    assert service.charge("acc_1", usd("2999.99")) is not None


def test_empty_approver_is_rejected(service, usd):
    with pytest.raises(PaymentError):
        service.charge("acc_1", usd("3500.00"), metadata={"approved_by": ""})


def test_api_maps_approval_required_to_403(api):
    response = api.create_payment({"account_id": "acc_1", "amount": "3000.00"})
    assert response.status == 403


def test_api_allows_approved_payment(api):
    response = api.create_payment(
        {"account_id": "acc_1", "amount": "3000.00", "metadata": {"approved_by": "risk"}}
    )
    assert response.status == 201


def test_domain_layer_does_not_import_upper_layers():
    """The dependency direction api -> services -> repositories -> domain must hold."""
    domain_dir = pathlib.Path(payments.__file__).parent / "domain"
    for path in domain_dir.glob("*.py"):
        source = path.read_text()
        for forbidden in ("payments.services", "payments.api", "payments.repositories"):
            assert forbidden not in source, (
                f"{path.name} imports {forbidden}: the domain layer must not depend on "
                "the layers above it"
            )
