"""Exact emulation of the x87 fsin/fcos instructions.

A copy of openevp/decoders/sony_lpec/x87.py plus two rounding helpers at the
end (round_to_float32, round_to_double) used by tables.py for the tone
tables. A copy rather than an import, so each decoder package stands on its
own (a build or a test can leave one out without breaking the other).

Plain double sin/cos (the platform libm) does not reproduce the x87
fsin/fcos results bit for bit: x87 reduces the argument using its own
~66-bit approximation of pi, evaluates the trig function to a 64-bit
mantissa (its native register format), and only rounds to a 53-bit double
when the result is stored. Two roundings happen, not one, and the
argument reduction itself is not exact math-pi reduction. This emulates
that: reduce the exact value of the input double by the nearest multiple
of pi/2, using pi rounded to 66 bits; evaluate sin/cos of the remainder to
far more than 64 bits (Decimal, well beyond the doc's ">= 128 bits");
round to a 64-bit mantissa; the caller rounds to a double last.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from fractions import Fraction

_PI_DIGITS = (
    "3.14159265358979323846264338327950288419716939937510582097494459230781"
    "640628620899862803482534211706798"
)


def _round_fraction_to_bits(value: Fraction, bits: int) -> Fraction:
    """Round a nonzero Fraction to `bits` significant bits (ties to even).

    Returns an exact Fraction (a finite binary fraction: numerator over a
    power of two), i.e. the value a `bits`-bit mantissa register would hold.
    """
    if value == 0:
        return Fraction(0)
    sign = 1 if value > 0 else -1
    value = abs(value)
    exponent = value.numerator.bit_length() - value.denominator.bit_length() - bits

    def scaled(e: int) -> Fraction:
        return value * Fraction(1, 1 << e) if e >= 0 else value * (1 << -e)

    scaled_value = scaled(exponent)
    while scaled_value < (1 << (bits - 1)):
        exponent -= 1
        scaled_value = scaled(exponent)
    while scaled_value >= (1 << bits):
        exponent += 1
        scaled_value = scaled(exponent)

    mantissa = round(scaled_value)  # Fraction.__round__ ties to even
    if mantissa == (1 << bits):
        mantissa = 1 << (bits - 1)
        exponent += 1

    magnitude = Fraction(mantissa, 1 << -exponent) if exponent < 0 else Fraction(mantissa * (1 << exponent))
    return sign * magnitude


_PI_EXACT = Fraction(Decimal(_PI_DIGITS))
_PI_66 = _round_fraction_to_bits(_PI_EXACT, 66)


def _sincos_decimal(x: Decimal, digits: int) -> tuple:
    """(sin(x), cos(x)) by Taylor series, to `digits` significant digits.

    `x` must already be small (the caller reduces it modulo pi/2 first), so
    the series converges in well under a hundred terms.
    """
    with localcontext() as ctx:
        ctx.prec = digits + 15
        x2 = x * x
        sin_term = x
        sin_sum = x
        cos_term = Decimal(1)
        cos_sum = Decimal(1)
        threshold = Decimal(1).scaleb(-(digits + 8))
        n = 0
        while True:
            n += 1
            sin_term = sin_term * (-x2) / ((2 * n) * (2 * n + 1))
            cos_term = cos_term * (-x2) / ((2 * n - 1) * (2 * n))
            sin_sum += sin_term
            cos_sum += cos_term
            if abs(sin_term) < threshold and abs(cos_term) < threshold:
                return sin_sum, cos_sum
            if n > 200:
                raise ArithmeticError("Taylor series for sin/cos did not converge")


def sincos_ext(x: float) -> tuple:
    """Emulate x87 fsin(x) and fcos(x), each rounded to a 64-bit mantissa.

    Returns (sin_ext, cos_ext) as exact Fractions in that "register"
    format; the caller rounds to a double (float(...)) for a normal result,
    or uses the extended value directly where docs/lpec.md says a formula
    needs the pre-rounding fcos (the sine-window tables).
    """
    exact_x = Fraction(x)
    half_pi = _PI_66 / 2
    n = round(exact_x / half_pi)  # Fraction.__round__ ties to even
    remainder = exact_x - n * half_pi

    with localcontext() as ctx:
        ctx.prec = 80
        remainder_dec = Decimal(remainder.numerator) / Decimal(remainder.denominator)
        sin_r, cos_r = _sincos_decimal(remainder_dec, 70)

    quadrant = n % 4
    sin_x = {0: sin_r, 1: cos_r, 2: -sin_r, 3: -cos_r}[quadrant]
    cos_x = {0: cos_r, 1: -sin_r, 2: -cos_r, 3: sin_r}[quadrant]

    return (
        _round_fraction_to_bits(Fraction(sin_x), 64),
        _round_fraction_to_bits(Fraction(cos_x), 64),
    )


def fsin(x: float) -> float:
    """x87 fsin(x), stored to a double."""
    sin_ext, _ = sincos_ext(x)
    return float(sin_ext)


def fcos_ext(x: float) -> Fraction:
    """fcos(x) at 64-bit-mantissa precision, not yet rounded to a double."""
    _, cos_ext = sincos_ext(x)
    return cos_ext


def fcos(x: float) -> float:
    """x87 fcos(x), stored to a double (docs/lpec.md, "Arithmetic")."""
    _, cos_ext = sincos_ext(x)
    return float(cos_ext)


def round_to_float32(value: Fraction) -> float:
    """A 64-bit-mantissa x87 register value stored to a float (fstp dword):
    rounded once, straight to 24 bits (ties to even)."""
    if value == 0:
        return 0.0
    return float(_round_fraction_to_bits(value, 24))


def round_to_double(value: Fraction) -> float:
    """An x87 arithmetic result at precision control 53 bits."""
    if value == 0:
        return 0.0
    return float(_round_fraction_to_bits(value, 53))
