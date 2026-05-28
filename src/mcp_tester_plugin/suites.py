"""Suite I/O: locate the ``mcp-suites/`` dir, load/list/save, schema-validate.

A *suite* is a committed YAML file describing a repeatable test: a set of
logical servers, an ordered list of steps (tool call + assertions + captures),
and teardown. Suites live in ``<repo-root>/mcp-suites/`` — deliberately
separate from any target MCP's own repo so the test engines never see MCP
source. In the mcp-test workspace the repo root is mcp-test/ itself.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from .assertions import ASSERTION_OPS, find_references

SCHEMA_VERSION = 1
SUITES_DIRNAME = "mcp-suites"
TARGETS_FILENAME = "targets.yaml"

# Pattern mirrors _PREFIX_RE in runner.py: mcp__ + one-or-more non-empty
# underscore-delimited segments + closing __.
_TOOL_PREFIX_RE = re.compile(r"^mcp__plugin_[^_][^_]*(?:_[^_][^_]*)*__")


class SuiteError(ValueError):
    """A suite file is structurally invalid."""


# --------------------------------------------------------------------------
# Locating the suites directory
# --------------------------------------------------------------------------
def find_root(start: Path | None = None) -> Path:
    """The repo root: an explicit override, else the nearest ``.git`` ancestor,
    else the current working directory."""
    override = os.environ.get("MCP_TESTER_ROOT")
    if override:
        return Path(override).resolve()
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / ".git").exists():
            return candidate
    return here


def suites_dir(root: Path | None = None) -> Path:
    override = os.environ.get("MCP_TESTER_SUITES_DIR")
    if override:
        return Path(override).resolve()
    return (root or find_root()) / SUITES_DIRNAME


def targets_path(root: Path | None = None) -> Path:
    return suites_dir(root) / TARGETS_FILENAME


def load_targets(root: Path | None = None) -> dict[str, Any] | None:
    path = targets_path(root)
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# --------------------------------------------------------------------------
# Load / list / save
# --------------------------------------------------------------------------
def load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SuiteError(f"suite not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        raise SuiteError(f"suite {path} is not a YAML mapping")
    validate(doc)
    return doc


def resolve_suite_path(name: str, root: Path | None = None) -> Path:
    """Accept a bare suite name, a filename, a full/relative path, or the
    ``suite:`` field value from any suite in the suites directory.

    Resolution order:
    1. ``name`` is an existing file path → return it directly.
    2. ``<suites-dir>/name``, ``<suites-dir>/name.yaml``, or
       ``<suites-dir>/name.yml`` exists → return the first match.
    3. Scan every ``*.y*ml`` file in the suites directory (excluding
       ``targets.yaml``) and return the first file whose ``suite:`` field
       exactly matches ``name``.  Corrupt / non-mapping files are silently
       skipped so one bad file cannot block resolution of a valid one.
    4. Raise :exc:`SuiteError` with the same message as before.
    """
    p = Path(name)
    if p.is_file():
        return p.resolve()
    sdir = suites_dir(root)
    for cand in (sdir / name, sdir / f"{name}.yaml", sdir / f"{name}.yml"):
        if cand.is_file():
            return cand.resolve()
    # Fallback: scan for a file whose `suite:` field matches `name`.
    if sdir.is_dir():
        for path in sorted(sdir.glob("*.y*ml")):
            if path.name == TARGETS_FILENAME:
                continue
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
                if isinstance(doc, dict) and doc.get("suite") == name:
                    return path.resolve()
            except Exception:  # noqa: BLE001
                continue
    raise SuiteError(f"no suite matching {name!r} under {sdir}")


def list_all(root: Path | None = None) -> list[dict[str, Any]]:
    sdir = suites_dir(root)
    if not sdir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(sdir.glob("*.y*ml")):
        if path.name == TARGETS_FILENAME:
            continue
        entry: dict[str, Any] = {"file": path.name, "path": str(path)}
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            entry["suite"] = doc.get("suite")
            entry["targets"] = doc.get("targets")
            entry["steps"] = len(doc.get("steps", []) or [])
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)
        out.append(entry)
    return out


def save(doc: dict[str, Any], root: Path | None = None, filename: str | None = None) -> Path:
    """Validate ``doc`` and write it into ``mcp-suites/``. Returns the path."""
    validate(doc)
    sdir = suites_dir(root)
    sdir.mkdir(parents=True, exist_ok=True)
    if filename is None:
        filename = default_filename(doc)
    if not filename.endswith((".yaml", ".yml")):
        filename += ".yaml"
    path = sdir / filename
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        yaml.safe_dump(doc, fh, sort_keys=False, allow_unicode=True)
    return path


def default_filename(doc: dict[str, Any]) -> str:
    """Derive ``<targets>__<suite-tail>.yaml`` from suite metadata."""
    suite = str(doc.get("suite") or "suite")
    # "agent-project-issues / ticket-lifecycle" -> "agent-project-issues__ticket-lifecycle"
    slug = re.sub(r"\s*/\s*", "__", suite.strip())
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", slug).strip("-")
    return f"{slug}.yaml"


# --------------------------------------------------------------------------
# Schema validation (structural + light dataflow)
# --------------------------------------------------------------------------
def validate(doc: dict[str, Any]) -> None:
    """Raise ``SuiteError`` on structural problems. Dataflow issues (a ``${var}``
    used before it is captured) are returned via :func:`dataflow_warnings`, not
    raised, so a recorder can still persist-then-verify."""
    if doc.get("schema") != SCHEMA_VERSION:
        raise SuiteError(
            f"unsupported schema {doc.get('schema')!r} (expected {SCHEMA_VERSION})"
        )
    if not doc.get("suite"):
        raise SuiteError("missing 'suite' name")

    servers = doc.get("servers")
    if not isinstance(servers, dict) or not servers:
        raise SuiteError("'servers' must be a non-empty mapping")
    for name, spec in servers.items():
        if not isinstance(spec, dict):
            raise SuiteError(f"server {name!r} must be a mapping")

    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        raise SuiteError("'steps' must be a non-empty list")

    seen_ids: set[str] = set()
    for block in ("steps", "teardown"):
        for i, step in enumerate(doc.get(block) or []):
            _validate_step(step, servers, block, i, seen_ids)


def _validate_step(
    step: Any, servers: dict[str, Any], block: str, idx: int, seen_ids: set[str]
) -> None:
    where = f"{block}[{idx}]"
    if not isinstance(step, dict):
        raise SuiteError(f"{where} must be a mapping")
    sid = step.get("id")
    if not sid:
        raise SuiteError(f"{where} missing 'id'")
    if sid in seen_ids:
        raise SuiteError(f"duplicate step id {sid!r}")
    seen_ids.add(sid)
    if not step.get("tool"):
        raise SuiteError(f"step {sid!r} missing 'tool'")
    srv = step.get("server")
    if srv is None and len(servers) == 1:
        srv = next(iter(servers))
    if srv not in servers:
        raise SuiteError(f"step {sid!r} references unknown server {srv!r}")
    for a in step.get("expect", []) or []:
        if not isinstance(a, dict) or "path" not in a or "op" not in a:
            raise SuiteError(f"step {sid!r} has a malformed assertion: {a!r}")
        if a["op"] not in ASSERTION_OPS:
            raise SuiteError(
                f"step {sid!r} uses unknown op {a['op']!r}; "
                f"valid: {sorted(ASSERTION_OPS)}"
            )
    cap = step.get("capture")
    if cap is not None and not isinstance(cap, dict):
        raise SuiteError(f"step {sid!r} 'capture' must be a mapping")


def dataflow_warnings(doc: dict[str, Any]) -> list[str]:
    """Best-effort: flag ``${var}`` references that are never bound earlier.

    ``RUN_ID``, ``env:*`` and ``sandbox.*`` are always considered bound. A
    capture binds its names for subsequent steps. Teardown sees everything the
    steps captured.

    Also warns when a step's ``tool:`` field contains a Claude Code harness
    prefix (``mcp__plugin_<...>__``). The runner strips the prefix at runtime,
    so the suite is still valid, but recording bare names is preferred.
    """
    warnings: list[str] = []
    bound: set[str] = {"RUN_ID"}
    sandbox = doc.get("sandbox") or {}
    for k in sandbox:
        bound.add(f"sandbox.{k}")

    def _check(step: dict[str, Any], where: str, available: set[str]) -> None:
        for ref in find_references({"args": step.get("args"), "expect": step.get("expect")}):
            if ref.startswith("env:") or ref.startswith("sandbox.") or ref == "RUN_ID":
                continue
            if ref not in available:
                warnings.append(f"{where}: '${{{ref}}}' used before it is captured")
        tool = step.get("tool") or ""
        if isinstance(tool, str) and _TOOL_PREFIX_RE.match(tool):
            warnings.append(
                f"{where}: tool {tool!r} contains a harness prefix; "
                "prefer recording the bare tool name"
            )

    for i, step in enumerate(doc.get("steps") or []):
        _check(step, f"steps[{i}]={step.get('id')}", bound)
        for name in (step.get("capture") or {}):
            bound.add(name)
    for i, step in enumerate(doc.get("teardown") or []):
        _check(step, f"teardown[{i}]={step.get('id')}", bound)
    return warnings
