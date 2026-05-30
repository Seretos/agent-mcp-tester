"""The deterministic replay engine + CLI dispatch.

Replays a recorded suite over raw MCP stdio JSON-RPC with zero LLM involvement:
resolve + spawn the referenced servers, initialize, then execute each step
(substitute vars -> call_tool -> evaluate assertions -> bind captures), always
run teardown, and emit a structured report whose ``regressions`` feed straight
into the ``file-findings`` skill.

This module owns ALL JSON-RPC and ALL process spawning. No LLM agent ever does.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import anyio
import yaml
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from . import assertions, report, resolve, suites


# --------------------------------------------------------------------------
# Exception-formatting and re-raise helpers (used at every catch site)
# --------------------------------------------------------------------------

def _format_exc(exc: BaseException) -> str:
    """Return a human-readable string for *exc*.

    Unwraps ``BaseExceptionGroup`` sub-exceptions (joined with ``"; "``);
    falls back to ``"TypeName: message"`` for everything else.
    """
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(f"{type(e).__name__}: {e}" for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


_FATAL = (KeyboardInterrupt, SystemExit, asyncio.CancelledError)


def _contains_fatal(exc: BaseException) -> bool:
    """Return True if *exc* is, or recursively contains, a fatal exception.

    Handles nested ``BaseExceptionGroup`` trees, which anyio produces during
    TaskGroup and AsyncExitStack teardown when a ``CancelledError`` is
    delivered as a group.
    """
    if isinstance(exc, _FATAL):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains_fatal(e) for e in exc.exceptions)
    return False


def _reraise_if_fatal(exc: BaseException) -> None:
    """Re-raise *exc* if it is a cancellation / interrupt / exit signal.

    Must be called at the top of every ``except BaseException`` block that
    would otherwise swallow the exception into a structured-error dict.
    Cancellation, keyboard-interrupt, and system-exit must always propagate
    so that anyio task-group teardown and MCP-server shutdown work correctly.

    Also handles ``BaseExceptionGroup`` wrapping — anyio delivers cancellation
    during TaskGroup/AsyncExitStack teardown as
    ``BaseExceptionGroup("...", [CancelledError()])`` whose top-level type is
    NOT ``CancelledError``.  Without this check the group would slip past the
    simple ``isinstance`` guard and be swallowed into a structured-error dict.
    """
    if isinstance(exc, _FATAL):
        raise
    if isinstance(exc, BaseExceptionGroup) and _contains_fatal(exc):
        raise


# --------------------------------------------------------------------------
# Exception unwrapping (PR #19 — opaque crash reporting)
# --------------------------------------------------------------------------
def _unwrap_exception(exc: Exception) -> str:
    """Return a readable error string, unwrapping ExceptionGroup wrappers.

    ``hasattr(exc, 'exceptions')`` covers both the Python 3.11+ built-in
    ``ExceptionGroup`` and the ``exceptiongroup`` backport anyio uses on 3.10.
    No version-conditional import needed.
    """
    if hasattr(exc, "exceptions") and exc.exceptions:
        inner = exc.exceptions[0]
        return (
            f"{type(exc).__name__}: {exc} | caused by: "
            f"{type(inner).__name__}: {inner}"
        )
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------
# Double-brace placeholder normalisation
# --------------------------------------------------------------------------
# Suites recorded by the LLM sweep use ``{{name}}`` (Jinja-style) for
# placeholders, but ``assertions.substitute`` only expands ``${name}``.
# Normalise at suite-load time so the rest of the engine is unaffected.
_DOUBLE_BRACE_RE = re.compile(r"\{\{([^}]+)\}\}")


def _normalize_placeholders(obj: Any) -> Any:
    """Recursively replace ``{{name}}`` with ``${name}`` in strings.

    - Handles nested dicts and lists (same recursion as ``assertions.substitute``).
    - Non-string scalars (int, bool, None) are returned unchanged.
    - Already-``${var}`` strings pass through without modification because they
      contain no ``{{...}}``, so the regex finds nothing.
    - ``{{env:MY_VAR}}`` → ``${env:MY_VAR}`` (env-var name case is preserved so
      that ``os.environ.get("MY_VAR")`` works on case-sensitive Linux).
    - For all names (including non-``env:``), only surrounding whitespace is
      stripped and the original case is preserved.  ``{{ Run_Id }}`` →
      ``${Run_Id}``, ``{{ticketId}}`` → ``${ticketId}``.  Capture keys are
      stored verbatim in ``variables``, so lowercasing would break lookup.
    - The ``run_id`` / ``RUN_ID`` dual-binding in ``_replay`` already covers the
      common canonicalisation need — no lowercasing is required here.
    - Idempotent: calling twice produces the same result.
    """
    if isinstance(obj, str):
        def _repl(m: re.Match) -> str:
            name = m.group(1).strip()
            if name.lower().startswith("env:"):
                # Preserve env-var name case; only normalise the "env:" prefix.
                # name[4:] is correct because "env:" is exactly 4 chars and
                # name is already stripped, so the tail is the raw var name.
                return "${env:" + name[4:] + "}"
            return "${" + name + "}"

        return _DOUBLE_BRACE_RE.sub(_repl, obj)
    if isinstance(obj, dict):
        return {k: _normalize_placeholders(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize_placeholders(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# Public entry points (also used by the FastMCP tools in server.py)
# --------------------------------------------------------------------------
def run(
    suite: str,
    *,
    root: Path | None = None,
    policy: str = "continue",
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = root or suites.find_root()
    path = suites.resolve_suite_path(suite, root)
    doc = suites.load(path)
    try:
        rep = anyio.run(_replay, doc, root, overrides or {}, policy)
    except Exception as exc:  # noqa: BLE001
        return {
            "result": "error",
            "error": _unwrap_exception(exc),
            "suite": doc.get("suite"),
            "suite_file": str(path),
            "run_id": None,
            "counts": {},
            "servers": [],
            "regressions": [],
        }
    rep["suite_file"] = str(path)
    return rep


def run_doc(
    doc: dict[str, Any],
    *,
    root: Path | None = None,
    policy: str = "continue",
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = root or suites.find_root()
    suites.validate(doc)
    return anyio.run(_replay, doc, root, overrides or {}, policy)


def validate_suite(
    suite: str,
    *,
    root: Path | None = None,
    verify_replay: bool = True,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = root or suites.find_root()
    is_inline = False
    try:
        path = suites.resolve_suite_path(suite, root)
    except suites.SuiteError:
        is_inline = True

    if is_inline:
        try:
            doc = yaml.safe_load(suite)
        except yaml.YAMLError as exc:
            raise suites.SuiteError(
                f"suite input is not valid YAML: {exc}"
            ) from exc
        if not isinstance(doc, dict):
            raise suites.SuiteError(
                "suite input is not a file path/name and does not parse as a YAML mapping"
            )
        suites.validate(doc)  # schema SuiteError propagates
        out: dict[str, Any] = {
            "valid": True,
            "inline": True,
            "dataflow_warnings": suites.dataflow_warnings(doc),
        }
    else:
        doc = suites.load(path)  # schema SuiteError propagates
        out = {
            "valid": True,
            "suite_file": str(path),
            "dataflow_warnings": suites.dataflow_warnings(doc),
        }
    if verify_replay:
        try:
            rep = anyio.run(_replay, doc, root, overrides or {}, "continue")
        except Exception as exc:  # noqa: BLE001
            # Schema validation already passed (out["valid"] is True).  A crash
            # here is a runtime problem at replay time — NOT a schema problem.
            # Preserve valid=True so callers (and cli_dispatch) honour the
            # "valid reflects schema validity only" contract.
            return {
                **out,
                "verify_replay": {"result": "error", "error": _unwrap_exception(exc)},
            }
        out["verify_replay"] = rep
    return out


def save_suite(
    suite_yaml: str,
    *,
    root: Path | None = None,
    verify_replay: bool = True,
    filename: str | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate, optionally verify-replay, then persist a recorded suite.

    If ``verify_replay`` is on and the replay does not pass, the suite is NOT
    written — the failing assertions are returned so the recorder can downgrade
    a mis-marked volatile field before committing.
    """
    root = root or suites.find_root()
    doc = yaml.safe_load(suite_yaml)
    if not isinstance(doc, dict):
        return {"saved": False, "error": "suite YAML is not a mapping"}
    try:
        suites.validate(doc)
    except suites.SuiteError as exc:
        return {"saved": False, "error": str(exc)}

    out: dict[str, Any] = {
        "saved": False,
        "dataflow_warnings": suites.dataflow_warnings(doc),
    }
    if verify_replay:
        rep = anyio.run(_replay, doc, root, overrides or {}, "continue")
        out["verify_replay"] = rep
        if rep.get("result") != "pass":
            out["error"] = "verify-replay did not pass; suite not saved"
            return out

    path = suites.save(doc, root=root, filename=filename)
    out["saved"] = True
    out["path"] = str(path)
    return out


# --------------------------------------------------------------------------
# Async entry points (called from within FastMCP's already-running event loop)
# --------------------------------------------------------------------------
async def run_async(
    suite: str,
    *,
    root: Path | None = None,
    policy: str = "continue",
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = root or suites.find_root()
    path = suites.resolve_suite_path(suite, root)
    doc = suites.load(path)
    rep = await _replay(doc, root, overrides or {}, policy)
    rep["suite_file"] = str(path)
    return rep


async def run_doc_async(
    doc: dict[str, Any],
    *,
    root: Path | None = None,
    policy: str = "continue",
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = root or suites.find_root()
    suites.validate(doc)
    return await _replay(doc, root, overrides or {}, policy)


async def validate_suite_async(
    suite: str,
    *,
    root: Path | None = None,
    verify_replay: bool = True,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    root = root or suites.find_root()
    is_inline = False
    try:
        path = suites.resolve_suite_path(suite, root)
    except suites.SuiteError:
        is_inline = True

    if is_inline:
        try:
            doc = yaml.safe_load(suite)
        except yaml.YAMLError as exc:
            raise suites.SuiteError(
                f"suite input is not valid YAML: {exc}"
            ) from exc
        if not isinstance(doc, dict):
            raise suites.SuiteError(
                "suite input is not a file path/name and does not parse as a YAML mapping"
            )
        suites.validate(doc)  # schema SuiteError propagates
        out: dict[str, Any] = {
            "valid": True,
            "inline": True,
            "dataflow_warnings": suites.dataflow_warnings(doc),
        }
    else:
        doc = suites.load(path)  # schema SuiteError propagates
        out = {
            "valid": True,
            "suite_file": str(path),
            "dataflow_warnings": suites.dataflow_warnings(doc),
        }
    if verify_replay:
        try:
            rep = await _replay(doc, root, overrides or {}, "continue")
        except BaseException as exc:  # noqa: BLE001
            # _replay's own outer guard normally catches all BaseExceptions, but
            # belt-and-suspenders: if anything escapes, surface it as an error
            # report rather than propagating and crashing the MCP tool.
            _reraise_if_fatal(exc)
            rep = {"result": "error", "error": _format_exc(exc)}
        out["verify_replay"] = rep
    return out


async def save_suite_async(
    suite_yaml: str,
    *,
    root: Path | None = None,
    verify_replay: bool = True,
    filename: str | None = None,
    overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate, optionally verify-replay, then persist a recorded suite.

    If ``verify_replay`` is on and the replay does not pass, the suite is NOT
    written — the failing assertions are returned so the recorder can downgrade
    a mis-marked volatile field before committing.
    """
    root = root or suites.find_root()
    doc = yaml.safe_load(suite_yaml)
    if not isinstance(doc, dict):
        return {"saved": False, "error": "suite YAML is not a mapping"}
    try:
        suites.validate(doc)
    except suites.SuiteError as exc:
        return {"saved": False, "error": str(exc)}

    out: dict[str, Any] = {
        "saved": False,
        "dataflow_warnings": suites.dataflow_warnings(doc),
    }
    if verify_replay:
        rep = await _replay(doc, root, overrides or {}, "continue")
        out["verify_replay"] = rep
        if rep.get("result") != "pass":
            out["error"] = "verify-replay did not pass; suite not saved"
            return out

    path = suites.save(doc, root=root, filename=filename)
    out["saved"] = True
    out["path"] = str(path)
    return out


# --------------------------------------------------------------------------
# The async replay core
# --------------------------------------------------------------------------
async def _replay(
    doc: dict[str, Any],
    root: Path,
    overrides: dict[str, str],
    policy: str,
) -> dict[str, Any]:
    started = time.time()
    run_id = _gen_run_id(doc)
    # Bind both "RUN_ID" (legacy dollar-brace form used in recorded suites)
    # and "run_id" (the new double-brace/normalised form) so that both
    # ${RUN_ID} and ${run_id} (from {{run_id}}) resolve correctly.
    variables: dict[str, Any] = {"run_id": run_id, "RUN_ID": run_id}
    for k, v in (doc.get("sandbox") or {}).items():
        variables[f"sandbox.{k}"] = v

    targets = suites.load_targets(root)
    server_specs: dict[str, dict[str, Any]] = doc.get("servers", {})

    # Which servers are actually referenced by steps + teardown?
    referenced = _referenced_servers(doc, server_specs)

    server_report: list[dict[str, Any]] = []
    step_results: list[dict[str, Any]] = []
    regressions: list[dict[str, Any]] = []
    teardown_warnings: list[str] = []

    child_env = _child_env()

    try:
        async with AsyncExitStack() as stack:
            sessions: dict[str, tuple[ClientSession, set[str], str]] = {}
            init_failed = False

            for logical in referenced:
                spec = server_specs[logical]
                entry: dict[str, Any] = {"logical": logical, "plugin": spec.get("plugin")}
                try:
                    launch = resolve.resolve(
                        logical, spec, root=root, overrides=overrides, targets=targets
                    )
                except resolve.ResolutionError as exc:
                    entry.update(resolved_via="unresolved", init_ok=False, error=str(exc))
                    server_report.append(entry)
                    init_failed = True
                    continue

                entry.update(
                    resolved_via=launch.source,
                    server=launch.server,
                    command=launch.command,
                )
                try:
                    params = StdioServerParameters(
                        command=launch.command,
                        args=launch.args,
                        env=child_env,
                        cwd=str(root),
                    )
                    read, write = await stack.enter_async_context(stdio_client(params))
                    session = await stack.enter_async_context(ClientSession(read, write))
                    await session.initialize()
                    listed = await session.list_tools()
                    toolnames = {t.name for t in listed.tools}
                    sessions[logical] = (session, toolnames, spec.get("plugin") or logical)
                    entry["init_ok"] = True
                    entry["tools"] = len(toolnames)
                except BaseException as exc:  # noqa: BLE001
                    _reraise_if_fatal(exc)
                    entry["init_ok"] = False
                    entry["error"] = _format_exc(exc)
                    init_failed = True
                server_report.append(entry)

            # ---- steps ----
            aborted = False
            for step in doc.get("steps", []):
                step = _normalize_placeholders(step)
                if aborted:
                    step_results.append({"id": step.get("id"), "status": "skipped",
                                         "reason": "aborted by policy"})
                    continue
                res = await _exec_step(
                    step, sessions, variables, regressions, is_teardown=False
                )
                step_results.append(res)
                if res["status"] == "fail" and policy == "abort":
                    aborted = True

            # ---- teardown (always) ----
            for step in doc.get("teardown", []) or []:
                step = _normalize_placeholders(step)
                res = await _exec_step(
                    step, sessions, variables, regressions, is_teardown=True
                )
                step_results.append(res)
                if res["status"] in ("fail", "skipped") and res.get("teardown_note"):
                    teardown_warnings.append(res["teardown_note"])
    except BaseException as exc:  # noqa: BLE001
        # An ExceptionGroup (or other BaseException) escaped the per-server guards
        # (e.g. during anyio TaskGroup teardown or step execution). Return a
        # structured report rather than propagating.
        _reraise_if_fatal(exc)
        err_msg = _format_exc(exc)
        return {
            "suite": doc.get("suite"),
            "run_id": run_id,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "duration_ms": int((time.time() - started) * 1000),
            "policy": policy,
            "result": "error",
            "phase": "execution",
            "error": err_msg,
            "counts": {
                "steps": len(step_results),
                "passed": 0,
                "failed": 0,
                "teardown_warnings": 0,
            },
            "servers": server_report,
            "steps": step_results,
            "regressions": regressions,
            "teardown_warnings": teardown_warnings,
        }

    passed = sum(1 for r in step_results if r["status"] == "pass")
    failed = sum(1 for r in step_results if r["status"] == "fail")

    if any(not s.get("init_ok") for s in server_report):
        result = "error"
    elif regressions:
        result = "regression"
    else:
        result = "pass"

    return {
        "suite": doc.get("suite"),
        "run_id": run_id,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
        "duration_ms": int((time.time() - started) * 1000),
        "policy": policy,
        "result": result,
        "counts": {
            "steps": len(step_results),
            "passed": passed,
            "failed": failed,
            "teardown_warnings": len(teardown_warnings),
        },
        "servers": server_report,
        "steps": step_results,
        "regressions": regressions,
        "teardown_warnings": teardown_warnings,
    }


async def _exec_step(
    step: dict[str, Any],
    sessions: dict[str, tuple[ClientSession, set[str], str]],
    variables: dict[str, Any],
    regressions: list[dict[str, Any]],
    *,
    is_teardown: bool,
) -> dict[str, Any]:
    sid = step.get("id")
    tool = _strip_tool_prefix(step.get("tool") or "")
    logical = step.get("server") or (next(iter(sessions)) if len(sessions) == 1 else None)
    out: dict[str, Any] = {"id": sid, "server": logical, "tool": tool}

    if logical not in sessions:
        out["status"] = "skipped"
        out["reason"] = f"server {logical!r} not initialized"
        if is_teardown:
            out["teardown_note"] = f"{sid}: server {logical!r} unavailable"
        return out
    session, toolnames, mcp_name = sessions[logical]

    # Substitute variables into args.
    try:
        args = assertions.substitute(step.get("args") or {}, variables)
    except assertions.UnresolvedVariable as exc:
        if is_teardown and step.get("on_missing_var") == "skip":
            out["status"] = "skipped"
            out["reason"] = f"unresolved ${{{exc}}} (on_missing_var: skip)"
            return out
        out["status"] = "fail"
        out["error"] = f"unresolved variable {exc}"
        regressions.append(report.make_regression(
            step_id=sid, server=logical, mcp=mcp_name, tool=tool, cls="harness",
            observed=f"unresolved variable {exc}", expected="all ${vars} bound",
            repro=report.repro_string(tool, step.get("args")), severity="med"))
        return out
    out["args"] = args

    # Contract check: is the tool even exposed?
    if tool not in toolnames:
        out["status"] = "fail"
        out["error"] = "tool not advertised by list_tools"
        regressions.append(report.make_regression(
            step_id=sid, server=logical, mcp=mcp_name, tool=tool, cls="contract",
            observed=f"{tool} absent from server's tool list",
            expected=f"{tool} exposed by {mcp_name}",
            repro=report.repro_string(tool, args), severity="high"))
        return out

    # Call the tool.
    try:
        result = await session.call_tool(tool, arguments=args)
    except Exception as exc:  # noqa: BLE001
        out["status"] = "fail"
        out["error"] = f"{type(exc).__name__}: {exc}"
        if is_teardown:
            out["teardown_note"] = f"{sid}: call failed ({exc})"
            return out
        regressions.append(report.make_regression(
            step_id=sid, server=logical, mcp=mcp_name, tool=tool, cls="harness",
            observed=f"call raised {type(exc).__name__}: {exc}",
            expected="tool call returns a result",
            repro=report.repro_string(tool, args), severity="med"))
        return out

    data, text, is_error = _parse_result(result)
    out["is_error"] = is_error
    out["result_excerpt"] = _excerpt(data)

    # Evaluate assertions.
    expects = step.get("expect") or []
    assertion_results: list[dict[str, Any]] = []
    for a in expects:
        a2 = dict(a)
        if "value" in a2:
            try:
                a2["value"] = assertions.substitute(a2["value"], variables)
            except assertions.UnresolvedVariable as exc:
                assertion_results.append({"path": a.get("path"), "op": a.get("op"),
                                          "ok": False, "error": f"unresolved {exc}"})
                continue
        assertion_results.append(assertions.evaluate(a2, data))
    out["assertions"] = assertion_results

    all_ok = all(a.get("ok") for a in assertion_results)
    # A tool-level error with no assertion explicitly inspecting it is a failure.
    if is_error and not expects:
        all_ok = False

    if not all_ok:
        out["status"] = "fail"
        if is_teardown:
            out["teardown_note"] = f"{sid}: teardown assertions failed"
            return out
        failed = [a for a in assertion_results if not a.get("ok")]
        observed = "; ".join(
            f"{a.get('path')} {a.get('op')} -> actual={a.get('actual')!r}"
            + (f" ({a['error']})" if a.get("error") else "")
            for a in failed
        ) or ("tool returned isError" if is_error else "assertion failed")
        expected = "; ".join(
            f"{a.get('path')} {a.get('op')} {a.get('value', '')}".strip()
            for a in failed
        )
        # If every failed assertion is a "path did not resolve" error the
        # root cause is a wrong JSONPath in the suite (suite-authoring defect),
        # not a behavioural regression in the MCP under test.
        # Guard: an empty failed list (e.g. is_error with no expect entries)
        # must NOT be treated as "all path misses" — that would mislabel an
        # is_error/no-expect failure as class="harness" instead of "behavioural".
        all_path_miss = bool(failed) and all(
            a.get("error") == "path did not resolve" for a in failed
        )
        assertion_cls = "harness" if all_path_miss else "behavioural"
        regressions.append(report.make_regression(
            step_id=sid, server=logical, mcp=mcp_name, tool=tool, cls=assertion_cls,
            observed=observed, expected=expected or "assertions hold",
            repro=report.repro_string(tool, args), severity="high"))
        return out

    # Bind captures.
    capture = step.get("capture") or {}
    captured: dict[str, Any] = {}
    for name, path in capture.items():
        found, value = assertions.extract_one(path, data)
        if not found:
            out["status"] = "fail"
            out["error"] = f"capture {name!r} path {path!r} did not resolve"
            # A capture path that does not resolve is always a suite-authoring
            # defect — the recorder wrote the wrong JSONPath.
            regressions.append(report.make_regression(
                step_id=sid, server=logical, mcp=mcp_name, tool=tool, cls="harness",
                observed=f"capture {name} <- {path} did not resolve",
                expected=f"{path} present in result",
                repro=report.repro_string(tool, args), severity="high"))
            return out
        variables[name] = value
        captured[name] = value
    if captured:
        out["captured"] = captured

    out["status"] = "pass"
    return out


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

# Pattern: mcp__ followed by one or more non-empty underscore-delimited
# segments, then a closing __.  Requires at least one segment so that a bare
# ``mcp__`` without a closing ``__`` is NOT stripped.
_PREFIX_RE = re.compile(r"^mcp__plugin_[^_][^_]*(?:_[^_][^_]*)*__")


def _strip_tool_prefix(name: str) -> str:
    """Strip the Claude Code harness prefix from a recorded tool name.

    Claude Code stores tool names as ``mcp__plugin_<plugin-name>__<bare-name>``
    in suite ``tool:`` fields, but raw MCP stdio advertises only the bare name.
    This helper normalises recorded names to their bare form so the runner can
    match them against the server's ``list_tools`` response.

    - Bare names (no prefix) pass through unchanged.
    - Names that START with ``mcp__`` but LACK a proper closing ``__`` are also
      returned unchanged (no over-eager strip).
    - Non-string inputs (e.g. an integer from a malformed YAML ``tool:`` field)
      are returned unchanged so callers can handle them gracefully downstream.
    - The function is idempotent.
    """
    if not name or not isinstance(name, str):
        return name
    m = _PREFIX_RE.match(name)
    if m:
        return name[m.end():]
    return name


def _referenced_servers(doc: dict[str, Any], specs: dict[str, Any]) -> list[str]:
    order: list[str] = []
    sole = next(iter(specs)) if len(specs) == 1 else None
    for block in ("steps", "teardown"):
        for step in doc.get(block) or []:
            logical = step.get("server") or sole
            if logical in specs and logical not in order:
                order.append(logical)
    return order


def _parse_result(result: Any) -> tuple[Any, str, bool]:
    structured = getattr(result, "structuredContent", None)
    texts: list[str] = []
    for block in getattr(result, "content", None) or []:
        t = getattr(block, "text", None)
        if t is not None:
            texts.append(t)
    text = "\n".join(texts)
    is_error = bool(getattr(result, "isError", False))

    data: Any = None
    if isinstance(structured, (dict, list)):
        data = structured
    elif text:
        try:
            data = json.loads(text)
        except Exception:  # noqa: BLE001
            data = None
    if not isinstance(data, (dict, list)):
        data = {}
    if isinstance(data, dict):
        data.setdefault("_text", text)
        data["_isError"] = is_error
    return data, text, is_error


def _excerpt(data: Any, limit: int = 600) -> Any:
    try:
        s = json.dumps(data, default=str)
    except Exception:  # noqa: BLE001
        s = str(data)
    if len(s) <= limit:
        try:
            return json.loads(s)
        except Exception:  # noqa: BLE001
            return s
    return s[:limit] + "…"


def _gen_run_id(doc: dict[str, Any]) -> str:
    cfg = doc.get("run_id") or {}
    template = cfg.get("template", "e2e-{{ts}}-{{rand6}}")
    return (
        template.replace("{{ts}}", str(int(time.time())))
        .replace("{{rand6}}", secrets.token_hex(3))
    )


def _child_env() -> dict[str, str]:
    """A copy of the host env with PyInstaller bootstrap vars scrubbed.

    The runner is (in release builds) a one-file PyInstaller binary spawning
    OTHER one-file binaries. PyInstaller injects ``_MEIPASS2`` / ``_PYI_*`` and
    rewrites ``LD_LIBRARY_PATH``; leaking those into the child makes the child's
    bootloader resolve the wrong libs. Scrub them. (No-op when run from source.)
    """
    env = dict(os.environ)
    env.pop("_MEIPASS2", None)
    for key in list(env):
        if key.startswith("_PYI"):
            env.pop(key, None)
    if "LD_LIBRARY_PATH_ORIG" in env:
        env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
    elif "LD_LIBRARY_PATH" in env:
        del env["LD_LIBRARY_PATH"]
    return env


def _parse_overrides(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"--server expects name=command, got {item!r}")
        name, cmd = item.split("=", 1)
        out[name.strip()] = cmd.strip()
    return out


# --------------------------------------------------------------------------
# CLI dispatch (no-arg path stays the MCP server; subcommands land here)
# --------------------------------------------------------------------------
def cli_dispatch(args: Any) -> int:
    if args.cmd == "list":
        entries = suites.list_all()
        print(json.dumps(entries, indent=2))
        return 0

    if args.cmd == "validate":
        try:
            out = validate_suite(
                args.suite,
                verify_replay=not args.no_replay,
                overrides=_parse_overrides(getattr(args, "server", None)),
            )
        except suites.SuiteError as exc:
            print(f"INVALID: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(out, indent=2, default=str))
        replay_result = out.get("verify_replay", {}).get("result", "pass")
        return 0 if (out.get("valid") and replay_result == "pass") else 1

    if args.cmd == "save":
        text = Path(args.file).read_text(encoding="utf-8")
        out = save_suite(
            text,
            verify_replay=not getattr(args, "no_replay", False),
            overrides=_parse_overrides(getattr(args, "server", None)),
        )
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("saved") else 1

    if args.cmd == "run":
        rep = run(
            args.suite,
            policy=args.policy,
            overrides=_parse_overrides(getattr(args, "server", None)),
        )
        if getattr(args, "report", None):
            Path(args.report).write_text(
                json.dumps(rep, indent=2, default=str), encoding="utf-8"
            )
        if getattr(args, "json", False):
            print(json.dumps(rep, indent=2, default=str))
        else:
            print(report.human_summary(rep))
        return 0 if rep.get("result") == "pass" else 1

    print(f"unknown command {args.cmd!r}", file=sys.stderr)
    return 2
