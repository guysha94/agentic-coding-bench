"""Hidden checks for the repository-wide audit.

Completeness is the point: these verify the report mentions EVERY module and EVERY error
class, which is exactly what an agent that loses track of earlier discoveries fails.
"""

import inspect
import pathlib

import payments
from payments.api.routes import ERROR_STATUS
from payments.domain import errors as errors_module

ROOT = pathlib.Path(payments.__file__).resolve().parent.parent.parent
SRC = pathlib.Path(payments.__file__).resolve().parent


def audit_text() -> str:
    path = ROOT / "AUDIT.md"
    assert path.exists(), "AUDIT.md was not created at the repository root"
    return path.read_text()


def error_classes() -> list[str]:
    return [
        name
        for name, obj in inspect.getmembers(errors_module, inspect.isclass)
        if issubclass(obj, errors_module.PaymentError) and obj is not errors_module.PaymentError
    ]


def test_audit_has_all_required_sections():
    text = audit_text()
    for heading in ("## Layers", "## Dependency direction", "## Error mapping", "## Findings"):
        assert heading in text, f"AUDIT.md is missing the {heading!r} section"


def test_audit_mentions_every_module():
    text = audit_text()
    missing = []
    for path in sorted(SRC.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        if path.stem not in text:
            missing.append(str(path.relative_to(SRC)))
    assert not missing, f"AUDIT.md does not mention these modules: {missing}"


def test_audit_mentions_every_layer():
    text = audit_text()
    for layer in ("api", "services", "repositories", "domain"):
        assert layer in text, f"AUDIT.md does not mention the {layer!r} layer"


def test_audit_mentions_every_error_class():
    text = audit_text()
    missing = [name for name in error_classes() if name not in text]
    assert not missing, f"AUDIT.md omits these error classes: {missing}"


def test_duplicate_payment_is_now_mapped():
    """The unmapped error the task asks to find."""
    assert errors_module.DuplicatePayment in ERROR_STATUS, (
        "DuplicatePayment is still unmapped and would surface as a 500"
    )
    assert ERROR_STATUS[errors_module.DuplicatePayment] == 409


def test_all_domain_errors_are_now_mapped():
    unmapped = [name for name in error_classes() if getattr(errors_module, name) not in ERROR_STATUS]
    assert not unmapped, f"these domain errors still have no API mapping: {unmapped}"


def test_public_service_methods_have_docstrings():
    from payments.services.payment_service import PaymentService

    missing = [
        name
        for name, method in inspect.getmembers(PaymentService, inspect.isfunction)
        if not name.startswith("_") and not (method.__doc__ or "").strip()
    ]
    assert not missing, f"PaymentService methods without docstrings: {missing}"


def test_public_api_methods_have_docstrings():
    from payments.api.routes import PaymentAPI

    missing = [
        name
        for name, method in inspect.getmembers(PaymentAPI, inspect.isfunction)
        if not name.startswith("_") and not (method.__doc__ or "").strip()
    ]
    assert not missing, f"PaymentAPI methods without docstrings: {missing}"
