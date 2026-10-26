from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal, Inexact, localcontext


MAX_DOMAIN_DECIMAL_TEXT_LENGTH = 256
MAX_EXACT_DECIMAL_PRECISION = 1_024


class DecimalDomainError(ValueError):
    """Raised when exact trading-domain Decimal arithmetic exceeds safe bounds."""


def validate_decimal_domain(value: Decimal, name: str) -> Decimal:
    """Validate a finite Decimal against bounded trading-domain limits.

    The bound is applied to the canonical fixed-point representation rather than
    the raw exponent/coefficient tuple, so semantically equivalent values such as
    ``1`` and ``1.000`` have identical acceptance behavior.
    """
    if not isinstance(value, Decimal):
        raise DecimalDomainError(f"{name} must be Decimal")
    if not value.is_finite():
        raise DecimalDomainError(f"{name} must be finite")

    # Import locally to keep canonical.py independent from arithmetic helpers.
    from .canonical import decimal_text

    try:
        text = decimal_text(value)
    except ValueError as exc:
        raise DecimalDomainError(f"{name} is outside supported Decimal bounds") from exc
    if len(text) > MAX_DOMAIN_DECIMAL_TEXT_LENGTH:
        raise DecimalDomainError(
            f"{name} exceeds maximum canonical Decimal length {MAX_DOMAIN_DECIMAL_TEXT_LENGTH}"
        )
    return value


def _normalized_parts(value: Decimal) -> tuple[tuple[int, ...], int]:
    _sign, raw_digits, raw_exponent = value.as_tuple()
    digits = list(raw_digits) or [0]
    if not isinstance(raw_exponent, int):
        raise DecimalDomainError("non-finite Decimal has no integer exponent")
    exponent = raw_exponent
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    return tuple(digits), exponent


def _addition_precision(left: Decimal, right: Decimal) -> int:
    left_digits, left_exponent = _normalized_parts(left)
    right_digits, right_exponent = _normalized_parts(right)
    common_exponent = min(left_exponent, right_exponent)
    left_width = len(left_digits) + left_exponent - common_exponent
    right_width = len(right_digits) + right_exponent - common_exponent
    return max(left_width, right_width) + 1


def _multiplication_precision(left: Decimal, right: Decimal) -> int:
    left_digits, _left_exponent = _normalized_parts(left)
    right_digits, _right_exponent = _normalized_parts(right)
    return max(1, len(left_digits) + len(right_digits))


def _checked_precision(required: int) -> int:
    if required > MAX_EXACT_DECIMAL_PRECISION:
        raise DecimalDomainError(
            f"exact Decimal operation exceeds maximum precision {MAX_EXACT_DECIMAL_PRECISION}"
        )
    return max(1, required)


def exact_add(left: Decimal, right: Decimal) -> Decimal:
    validate_decimal_domain(left, "left operand")
    validate_decimal_domain(right, "right operand")
    precision = _checked_precision(_addition_precision(left, right))
    with localcontext() as context:
        context.prec = precision
        context.traps[Inexact] = True
        result = left + right
    return validate_decimal_domain(result, "Decimal addition result")


def exact_subtract(left: Decimal, right: Decimal) -> Decimal:
    validate_decimal_domain(left, "left operand")
    validate_decimal_domain(right, "right operand")
    precision = _checked_precision(_addition_precision(left, right))
    with localcontext() as context:
        context.prec = precision
        context.traps[Inexact] = True
        result = left - right
    return validate_decimal_domain(result, "Decimal subtraction result")


def exact_multiply(left: Decimal, right: Decimal) -> Decimal:
    validate_decimal_domain(left, "left operand")
    validate_decimal_domain(right, "right operand")
    precision = _checked_precision(_multiplication_precision(left, right))
    with localcontext() as context:
        context.prec = precision
        context.traps[Inexact] = True
        result = left * right
    return validate_decimal_domain(result, "Decimal multiplication result")


def exact_sum(values: Iterable[Decimal], start: Decimal = Decimal("0")) -> Decimal:
    total = validate_decimal_domain(start, "sum start")
    for value in values:
        total = exact_add(total, value)
    return total


def exact_abs_difference(left: Decimal, right: Decimal) -> Decimal:
    return exact_subtract(left, right).copy_abs()
