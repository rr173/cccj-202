"""Declared unit table: dimension + positive rational factor per unit.

All quantity arithmetic is exact (fractions.Fraction).  A unit maps to a
dimension and a factor (value of 1 unit in the dimension's base unit).
Compatible-dimension conversion multiplies by factor ratios; there is no
float tolerance anywhere, so an over-quantity DELETE can never be
swallowed by rounding.
"""
from __future__ import annotations

import json
from fractions import Fraction


class UnitTableError(ValueError):
    pass


class UnitTable:
    def __init__(self, units: dict):
        # units: {code: {"dimension": str, "factor": Fraction}}
        self._units = dict(units)
        # base unit per dimension: any unit whose factor == 1; otherwise the
        # first declared unit of the dimension acts as the reference.
        self._base = {}
        for code, rec in self._units.items():
            dim = rec["dimension"]
            if dim not in self._base or rec["factor"] == 1:
                self._base[dim] = code

    @classmethod
    def from_dict(cls, data: dict) -> "UnitTable":
        raw = data.get("units")
        if not isinstance(raw, dict) or not raw:
            raise UnitTableError("unit table must contain a non-empty 'units' object")
        units = {}
        for code, rec in raw.items():
            if not isinstance(rec, dict):
                raise UnitTableError(f"unit {code!r}: record must be an object")
            dim = rec.get("dimension")
            if not isinstance(dim, str) or not dim:
                raise UnitTableError(f"unit {code!r}: missing dimension")
            if "factor" not in rec:
                raise UnitTableError(f"unit {code!r}: missing factor")
            factor = _parse_rational(rec["factor"], what=f"unit {code!r} factor")
            if factor <= 0:
                raise UnitTableError(f"unit {code!r}: factor must be a positive rational")
            units[code] = {"dimension": dim, "factor": factor}
        return cls(units)

    @classmethod
    def from_file(cls, path: str) -> "UnitTable":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_dict(json.load(fh))

    def has(self, code: str) -> bool:
        return code in self._units

    def dimension(self, code: str) -> str:
        return self._units[code]["dimension"]

    def base_unit(self, dimension: str) -> str:
        return self._base[dimension]

    def to_base(self, quantity: Fraction, code: str) -> tuple[Fraction, str, str]:
        """Return (quantity_in_base_unit, base_unit, dimension)."""
        rec = self._units[code]
        dim = rec["dimension"]
        base = self._base[dim]
        base_factor = self._units[base]["factor"]
        return quantity * rec["factor"] / base_factor, base, dim


def _parse_rational(value, what: str = "rational") -> Fraction:
    """Parse an exact positive rational from str/int (never via float)."""
    if isinstance(value, bool):
        raise UnitTableError(f"{what}: booleans are not rationals")
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, Fraction):
        return value
    if isinstance(value, float):
        raise UnitTableError(f"{what}: floats are not accepted; use a string like \"1/1000\"")
    if isinstance(value, str):
        text = value.strip()
        try:
            if "/" in text:
                num, den = text.split("/", 1)
                return Fraction(int(num.strip()), int(den.strip()))
            # decimal string -> exact via Fraction's decimal parser
            return Fraction(text)
        except (ValueError, ZeroDivisionError) as exc:
            raise UnitTableError(f"{what}: cannot parse {value!r} as a rational") from exc
    raise UnitTableError(f"{what}: unsupported rational {value!r}")


def parse_quantity(value, what: str = "quantity") -> Fraction:
    """Parse a declared quantity exactly.

    Accepts int, Fraction, Decimal (exact), float (recovered through the
    shortest-round-trip decimal representation, which is exact for values
    that originated as JSON decimal literals) and decimal strings.
    """
    import decimal

    if isinstance(value, bool):
        raise UnitTableError(f"{what}: booleans are not quantities")
    if isinstance(value, Fraction):
        return value
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, decimal.Decimal):
        return Fraction(value)
    if isinstance(value, float):
        # shortest round-trip repr reproduces the original decimal literal
        return Fraction(decimal.Decimal(repr(value)))
    if isinstance(value, str):
        try:
            return Fraction(value.strip())
        except ValueError as exc:
            raise UnitTableError(f"{what}: cannot parse {value!r}") from exc
    raise UnitTableError(f"{what}: unsupported value {value!r}")


def fmt_fraction(fr: Fraction) -> str:
    """Canonical exact string: '5' for integers, '24/5' otherwise."""
    if fr.denominator == 1:
        return str(fr.numerator)
    return f"{fr.numerator}/{fr.denominator}"
