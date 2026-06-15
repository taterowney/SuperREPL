"""Lean type rendering → JSON-Schema translation for the ``bridge`` manifest.

The Lean ``bridge`` describes each exposed method's arguments as a map from
argument name to a *Lean* type rendering (``Nat``, ``List String``,
``Option Int``, ...), not a JSON-Schema type. The helpers here turn those
renderings into JSON-Schema fragments so callers get a usable schema for the
arguments they must send.

Lean types with a clean JSON analogue are translated faithfully; everything
else falls back to "accept any JSON", with the original Lean type preserved in
the field description so the caller still knows what Lean will ``FromJson``-decode.
"""

from __future__ import annotations

from typing import Any

__all__ = ["OPEN_SCHEMA", "lean_type_to_schema", "manifest_to_input_schema"]


# Permissive schema: ``args`` is a JSON object keyed by argument name, but when
# a method's argument types are unknown we advertise an open object.
OPEN_SCHEMA: dict[str, Any] = {"type": "object"}

# Lean scalar types with a clean JSON-Schema analogue. Anything not listed (and
# not a recognized ``List``/``Array``/``Option`` wrapper below) falls back to
# "accept any JSON", with the Lean type preserved in the field description.
_LEAN_SCALARS: dict[str, dict[str, Any]] = {
    "Nat": {"type": "integer", "minimum": 0},
    "Int": {"type": "integer"},
    "USize": {"type": "integer", "minimum": 0},
    "UInt8": {"type": "integer", "minimum": 0},
    "UInt16": {"type": "integer", "minimum": 0},
    "UInt32": {"type": "integer", "minimum": 0},
    "UInt64": {"type": "integer", "minimum": 0},
    "String": {"type": "string"},
    "Char": {"type": "string"},
    "Name": {"type": "string"},
    "Lean.Name": {"type": "string"},
    "Bool": {"type": "boolean"},
    "Float": {"type": "number"},
    "Json": {},  # arbitrary JSON
    "Lean.Json": {},
    "Unit": {"type": "object"},
}


def _strip_outer_parens(t: str) -> str:
    """Remove a single fully-enclosing pair of parentheses, e.g. ``(List Nat)``
    -> ``List Nat``. Leaves ``(a) × (b)`` untouched (the outer parens there do
    not enclose the whole string)."""
    t = t.strip()
    while len(t) >= 2 and t[0] == "(" and t[-1] == ")":
        depth = 0
        for i, ch in enumerate(t):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0 and i != len(t) - 1:
                    return t  # the opening paren closes before the end
        t = t[1:-1].strip()
    return t


def lean_type_to_schema(lean_type: str) -> dict[str, Any]:
    """Translate a Lean type rendering into a JSON-Schema fragment.

    Handles scalars, ``List``/``Array`` (-> array), and ``Option`` (-> nullable).
    Unknown or structurally complex types (products, custom structures, ...)
    fall back to an empty schema that accepts any JSON; the Lean type itself is
    surfaced to the caller via the field description at the property level.
    """
    t = _strip_outer_parens(lean_type)

    if t in _LEAN_SCALARS:
        return dict(_LEAN_SCALARS[t])

    for ctor in ("List", "Array", r"List.{0}", r"Array.{0}"):
        prefix = ctor + " "
        if t.startswith(prefix):
            inner = t[len(prefix):].strip()
            return {"type": "array", "items": lean_type_to_schema(inner)}

    if t.startswith("Option "):
        inner = t[len("Option "):].strip()
        sub = lean_type_to_schema(inner)
        # Present but possibly null (the bridge still requires the key).
        return {"anyOf": [sub, {"type": "null"}]}

    # Lean-only / unrecognized: accept anything.
    return {}


def manifest_to_input_schema(arg_types: dict[str, str]) -> dict[str, Any]:
    """Build a JSON-Schema object from a method's ``input_schema`` arg map.

    Every argument is ``required``: the bridge looks each one up by key and
    errors on a missing key (``Option`` args must be present but may be null).
    The original Lean type is recorded in each property's ``description``.
    """
    if not arg_types:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    properties: dict[str, Any] = {}
    required: list[str] = []
    for arg, lean_type in arg_types.items():
        sub = lean_type_to_schema(lean_type)
        note = f"Lean type: {lean_type}"
        sub["description"] = f"{sub['description']} ({note})" if sub.get("description") else note
        properties[arg] = sub
        required.append(arg)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
