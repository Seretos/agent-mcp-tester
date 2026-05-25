"""Run-report assembly + mapping deterministic regressions to the findings shape.

The runner produces a structured report (JSON-serialisable dict). The same
report exposes a ``regressions`` list shaped 1:1 to what the ``file-findings``
skill consumes (tool, observed-vs-expected, repro, severity, routing), so a
deterministic replay can feed the exact same ticket-filing pipe as an LLM sweep.
"""

from __future__ import annotations

from typing import Any

# A regression's `class` (cause family) maps to file-findings' `routing` hint.
CLASS_TO_ROUTING = {
    "behavioural": "behaviour",
    "contract": "tool-surface",
    "schema-drift": "tool-surface",
    "setup": "tool-surface",
    "harness": "tool-surface",
}


def repro_string(tool: str, args: dict[str, Any] | None) -> str:
    """A compact, human-readable repro call for a finding body."""
    args = args or {}
    rendered = ", ".join(f"{k}={_abbrev(v)}" for k, v in args.items())
    return f"{tool}({rendered})"


def _abbrev(value: Any, limit: int = 40) -> str:
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"


def make_regression(
    *,
    step_id: str,
    server: str,
    mcp: str,
    tool: str,
    cls: str,
    observed: str,
    expected: str,
    repro: str,
    severity: str,
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "server": server,
        "mcp": mcp,
        "tool": tool,
        "class": cls,
        "observed": observed,
        "expected": expected,
        "repro": repro,
        "severity": severity,
        "routing": CLASS_TO_ROUTING.get(cls, "tool-surface"),
    }


def human_summary(report: dict[str, Any]) -> str:
    """A terse multi-line summary for stdout / a skill to relay."""
    lines: list[str] = []
    lines.append(f"suite : {report.get('suite')}")
    lines.append(f"run_id: {report.get('run_id')}")
    lines.append(f"result: {report.get('result')}")
    counts = report.get("counts", {})
    lines.append(
        "steps : {passed}/{steps} passed, {failed} failed, "
        "{teardown_warnings} teardown warnings".format(
            steps=counts.get("steps", 0),
            passed=counts.get("passed", 0),
            failed=counts.get("failed", 0),
            teardown_warnings=counts.get("teardown_warnings", 0),
        )
    )
    for srv in report.get("servers", []):
        lines.append(
            f"  server {srv.get('logical')} <- {srv.get('plugin')} "
            f"({srv.get('resolved_via')}, init_ok={srv.get('init_ok')})"
        )
    regs = report.get("regressions", [])
    if regs:
        lines.append("regressions:")
        for r in regs:
            lines.append(
                f"  [{r['severity']}/{r['class']}] {r['tool']} ({r['step_id']}): "
                f"{r['observed']}"
            )
    else:
        lines.append("regressions: none")
    return "\n".join(lines)
