from __future__ import annotations

from contextvars import ContextVar
from decimal import (
    Context,
    Decimal,
    DecimalException,
    MAX_EMAX,
    MIN_EMIN,
    ROUND_HALF_EVEN,
    getcontext,
    localcontext,
)
from functools import wraps
from typing import Callable, ParamSpec, TypeVar


def canonical_decimal_text(value: Decimal) -> str:
    """Return an exact, context-independent compact Decimal representation."""

    if type(value) is not Decimal:
        raise TypeError("value must be a Decimal")
    if not value.is_finite():
        raise ValueError("value must be finite")
    if value == 0:
        return "0"
    with localcontext() as context:
        # Decimal.normalize() otherwise uses the process context and may round a
        # long coefficient or overflow solely because Emax is too small.
        context.prec = max(1, len(value.as_tuple().digits))
        context.Emax = MAX_EMAX
        context.Emin = MIN_EMIN
        return str(value.normalize())


def parse_canonical_decimal_text(value: object, name: str = "decimal") -> Decimal:
    """Parse the exact finite Decimal spelling emitted by ``str(Decimal)``.

    Checkpoints must not accept alternate spellings such as ``+1``, ``01`` or
    lowercase exponent markers and silently rewrite them on the next snapshot.
    Trailing zeros and exponent notation remain valid when they are already the
    exact representation returned by ``str(Decimal(value))``.
    """

    if type(value) is not str:
        raise TypeError(f"{name} must be a canonical decimal string")
    try:
        parsed = Decimal(value)
    except DecimalException as exc:
        raise ValueError(f"{name} must be a finite decimal-compatible value") from exc
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite")
    if str(parsed) != value:
        raise ValueError(f"{name} must use canonical Decimal text")
    return parsed


_FVG_ARITHMETIC_CONTEXT = Context(
    prec=28,
    rounding=ROUND_HALF_EVEN,
    capitals=1,
    clamp=0,
)
_P = ParamSpec("_P")
_R = TypeVar("_R")
_FVG_CONTEXT_DEPTH: ContextVar[int] = ContextVar("fvg_decimal_context_depth", default=0)


def _has_fvg_arithmetic_settings(context: Context) -> bool:
    return (
        context.prec == _FVG_ARITHMETIC_CONTEXT.prec
        and context.rounding == _FVG_ARITHMETIC_CONTEXT.rounding
        and context.Emin == _FVG_ARITHMETIC_CONTEXT.Emin
        and context.Emax == _FVG_ARITHMETIC_CONTEXT.Emax
        and context.capitals == _FVG_ARITHMETIC_CONTEXT.capitals
        and context.clamp == _FVG_ARITHMETIC_CONTEXT.clamp
        and context.traps == _FVG_ARITHMETIC_CONTEXT.traps
    )


def _restore_flags(context: Context, flags: dict[type[DecimalException], bool]) -> None:
    for signal, enabled in flags.items():
        context.flags[signal] = enabled


def deterministic_decimal(function: Callable[_P, _R]) -> Callable[_P, _R]:
    """Run FVG arithmetic in a fixed context without leaking process state.

    The outermost decorated call owns a local Decimal context. Nested FVG calls
    reuse it through a ContextVar so callers keep their precision, flags and
    traps while the hot path avoids stacking redundant local contexts.
    """

    @wraps(function)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        depth = _FVG_CONTEXT_DEPTH.get()
        if depth:
            return function(*args, **kwargs)
        token = _FVG_CONTEXT_DEPTH.set(1)
        caller = getcontext()
        if _has_fvg_arithmetic_settings(caller):
            flags = dict(caller.flags)
            try:
                return function(*args, **kwargs)
            finally:
                _restore_flags(caller, flags)
                _FVG_CONTEXT_DEPTH.reset(token)
        try:
            with localcontext(_FVG_ARITHMETIC_CONTEXT):
                return function(*args, **kwargs)
        finally:
            _FVG_CONTEXT_DEPTH.reset(token)

    return wrapped
