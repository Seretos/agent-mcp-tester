"""Variable templating + assertion evaluation for the deterministic runner.

This module is pure (no I/O, no MCP). It powers two things the runner needs:

* ``substitute`` — recursively expand ``${var}`` / ``${env:NAME}`` references in a
  step's ``args`` (and in an assertion operand) against the live variable table.
* ``evaluate`` — check one ``{path, op, value}`` assertion against a parsed tool
  result using a small JSONPath subset.

The assertion vocabulary is deliberately small and behavioural — it exists to
catch *regressions* in a recorded suite, not to express arbitrary logic.
"""

from __future__ import annotations

import re
from typing import Any

from jsonpath_ng import parse as _jsonpath_parse

# Operators a suite's `expect` block may use. Kept in sync with suites.py's
# schema validation and the recorder guidance in cluster-tester.md.
ASSERTION_OPS = frozenset(
    {
        "exists",
        "absent",
        "not_null",
        "equals",
        "not_equals",
        "in",
        "contains",
        "matches",
        "type",
        "length",
        "gt",
        "ge",
        "lt",
        "le",
    }
)

_VAR_RE = re.compile(r"\$\{([^}]+)\}")


class UnresolvedVariable(KeyError):
    """A ``${...}`` reference had no binding at substitution time."""


# --------------------------------------------------------------------------
# Variable templating
# --------------------------------------------------------------------------
def substitute(obj: Any, variables: dict[str, Any]) -> Any:
    """Recursively expand ``${var}`` references in strings within ``obj``.

    A string that is *exactly* one reference (``"${ticket_id}"``) returns the
    bound value with its original type preserved (so a captured int stays an
    int). A string with embedded references (``"ticket ${RUN_ID}"``) returns a
    string with each reference stringified.
    """
    if isinstance(obj, str):
        whole = _VAR_RE.fullmatch(obj.strip())
        if whole is not None:
            return _lookup(whole.group(1), variables)

        def _repl(mo: re.Match[str]) -> str:
            return str(_lookup(mo.group(1), variables))

        return _VAR_RE.sub(_repl, obj)
    if isinstance(obj, dict):
        return {k: substitute(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, variables) for v in obj]
    return obj


def _lookup(name: str, variables: dict[str, Any]) -> Any:
    import os

    name = name.strip()
    if name.startswith("env:"):
        value = os.environ.get(name[4:])
        if value is None:
            raise UnresolvedVariable(name)
        return value
    if name in variables:
        return variables[name]
    raise UnresolvedVariable(name)


def find_references(obj: Any) -> set[str]:
    """Collect every ``${name}`` reference appearing in ``obj`` (recursively)."""
    found: set[str] = set()
    if isinstance(obj, str):
        found.update(m.group(1).strip() for m in _VAR_RE.finditer(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            found |= find_references(v)
    elif isinstance(obj, list):
        for v in obj:
            found |= find_references(v)
    return found


# --------------------------------------------------------------------------
# JSONPath extraction
# --------------------------------------------------------------------------
def jsonpath_all(path: str, data: Any) -> list[Any]:
    """Return every value matching ``path`` (a JSONPath expression)."""
    try:
        expr = _jsonpath_parse(path)
    except Exception as exc:  # noqa: BLE001 - surface a clean message
        raise ValueError(f"invalid JSONPath {path!r}: {exc}") from exc
    return [m.value for m in expr.find(data)]


def extract_one(path: str, data: Any) -> tuple[bool, Any]:
    """Return ``(found, value)`` for the first match of ``path`` in ``data``."""
    matches = jsonpath_all(path, data)
    if matches:
        return True, matches[0]
    return False, None


_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "bool": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


# --------------------------------------------------------------------------
# Assertion evaluation
# --------------------------------------------------------------------------
def evaluate(assertion: dict[str, Any], data: Any) -> dict[str, Any]:
    """Evaluate one assertion against ``data``.

    ``assertion`` is ``{"path": str, "op": str, "value"?: Any}`` with the
    operand already variable-substituted by the caller. Returns a result dict
    ``{path, op, value?, ok, actual}`` suitable for the report.
    """
    path = assertion["path"]
    op = assertion["op"]
    operand = assertion.get("value")

    if op not in ASSERTION_OPS:
        return {"path": path, "op": op, "ok": False, "error": f"unknown op {op!r}"}

    found, actual = extract_one(path, data)
    out: dict[str, Any] = {"path": path, "op": op, "actual": actual}
    if operand is not None:
        out["value"] = operand

    if op == "exists":
        out["ok"] = found
        return out
    if op == "absent":
        out["ok"] = not found
        return out
    if op == "not_null":
        out["ok"] = found and actual is not None
        return out

    # All remaining ops require the path to resolve first.
    if not found:
        out["ok"] = False
        out["error"] = "path did not resolve"
        return out

    try:
        out["ok"] = _compare(op, actual, operand)
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False
        out["error"] = str(exc)
    return out


def _compare(op: str, actual: Any, operand: Any) -> bool:
    if op == "equals":
        return actual == operand
    if op == "not_equals":
        return actual != operand
    if op == "in":
        return actual in (operand or [])
    if op == "contains":
        if isinstance(actual, str):
            return str(operand) in actual
        if isinstance(actual, (list, tuple, dict)):
            return operand in actual
        return False
    if op == "matches":
        return re.search(str(operand), str(actual)) is not None
    if op == "type":
        check = _TYPE_CHECKS.get(str(operand))
        if check is None:
            raise ValueError(f"unknown type {operand!r}")
        return check(actual)
    if op == "length":
        return _length_compare(actual, operand)
    if op in ("gt", "ge", "lt", "le"):
        a = float(actual)
        b = float(operand)
        return {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]
    raise ValueError(f"unhandled op {op!r}")


def _length_compare(actual: Any, operand: Any) -> bool:
    try:
        n = len(actual)
    except TypeError as exc:
        raise ValueError("value has no length") from exc
    # operand is either a bare int (== check) or {"op": "==|>=|<=", "value": N}
    if isinstance(operand, dict):
        cmp = operand.get("op", "==")
        target = operand["value"]
        return {
            "==": n == target,
            ">=": n >= target,
            "<=": n <= target,
            ">": n > target,
            "<": n < target,
        }[cmp]
    return n == operand
