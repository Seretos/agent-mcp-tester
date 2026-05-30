"""Dual-mode entry point for agent-mcp-tester.

* **No args** -> runs as a FastMCP stdio server (what Claude Code launches).
  Exposes the deterministic runner as tools the in-harness skills call.
* **A subcommand** (`run`, `list`, `validate`, `save`, `serve`) -> a plain CLI
  usable in CI with zero Claude.

Both surfaces call the same shared core in ``runner`` / ``suites``.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mcp-tester")


@mcp.tool()
def ping() -> str:
    """Health check tool. Returns 'pong' if the server is alive."""
    return "pong"


@mcp.tool()
async def run_suite(suite: str, policy: str = "continue") -> dict:
    """Replay a recorded suite deterministically over raw MCP stdio (zero LLM).

    `suite` accepts:
    - a filename stem (e.g. ``agent-worktree__worktree-lifecycle``),
    - a file path under ``mcp-suites/`` (relative or absolute), or
    - the human-readable ``suite:`` field value returned by ``list_suites``
      (e.g. ``"agent-worktree / worktree-lifecycle"``).

    `policy` is "continue" (run all steps, collect every regression) or
    "abort" (stop at first failure). Returns a structured report whose
    `regressions` feed the file-findings skill.
    """
    from . import runner

    try:
        return await runner.run_async(suite, policy=policy)
    except Exception as exc:  # noqa: BLE001
        return {
            "result": "error",
            "error": runner._unwrap_exception(exc),
            "suite": suite,
            "run_id": None,
            "counts": {},
            "servers": [],
            "regressions": [],
        }


@mcp.tool()
def list_suites() -> list:
    """List the committed suites under mcp-suites/ (name, targets, step count)."""
    from . import suites

    return suites.list_all()


@mcp.tool()
async def validate_suite(suite: str, verify_replay: bool = True) -> dict:
    """Schema-validate a suite without writing to disk.

    `suite` accepts either:
    - a saved-suite name or path under mcp-suites/ (resolved from disk), or
    - raw YAML text of a suite document (write-free schema check, useful for
      validating a draft before calling save_suite).

    A valid suite document must include a top-level ``schema: 1`` field.
    Omitting it or using any other value will raise a validation error.

    If `verify_replay` is True (default), the suite is also replayed once
    against the live MCP to confirm its assertions hold.

    Returns a dict with `valid` (bool, reflects schema validity only) and
    `dataflow_warnings`. When loaded from a file, `suite_file` is included.
    When parsed from inline YAML, `inline: true` is included instead and no
    file is written. When `verify_replay` is True, a `verify_replay` key
    contains the replay report; replay pass/fail does NOT affect `valid`.
    """
    from . import runner, suites

    try:
        return await runner.validate_suite_async(suite, verify_replay=verify_replay)
    except suites.SuiteError as exc:
        # Schema/structure validation failed — valid=False is correct here.
        return {
            "valid": False,
            "error": str(exc),
            "suite": suite,
        }
    except Exception as exc:  # noqa: BLE001
        # Runtime crash (e.g. ExceptionGroup from _replay, OS error, etc.)
        # that occurred AFTER schema validation passed.  The suite is
        # schema-valid; only the replay crashed.  Preserve valid=True per the
        # documented contract ("valid reflects schema validity only").
        return {
            "valid": True,
            "error": runner._unwrap_exception(exc),
            "suite": suite,
        }


@mcp.tool()
async def save_suite(suite_yaml: str, verify_replay: bool = True, filename: str = "") -> dict:
    """Persist a recorded suite (YAML text) into mcp-suites/.

    The suite YAML must include a top-level ``schema: 1`` field; omitting it
    or using any other value will cause validation to fail before saving.

    With verify_replay on (default), the suite is replayed once before being
    written; if the replay does not pass, the suite is NOT saved and the failing
    assertions are returned so a mis-marked volatile field can be downgraded.
    """
    from . import runner

    return await runner.save_suite_async(
        suite_yaml, verify_replay=verify_replay, filename=filename or None
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcp-tester",
        description="Deterministic MCP suite runner (and FastMCP server when run with no args).",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="replay a suite deterministically")
    pr.add_argument("suite", help="suite name or path under mcp-suites/")
    pr.add_argument("--policy", choices=["continue", "abort"], default="continue")
    pr.add_argument("--server", action="append", default=[],
                    help="override a server: name=command (repeatable)")
    pr.add_argument("--report", default=None, help="write the JSON report to this path")
    pr.add_argument("--json", action="store_true", help="print the full JSON report")

    sub.add_parser("list", help="list committed suites")

    pv = sub.add_parser("validate", help="schema-validate (and optionally replay) a suite")
    pv.add_argument("suite")
    pv.add_argument("--no-replay", action="store_true", help="skip verify-replay")
    pv.add_argument("--server", action="append", default=[])

    psave = sub.add_parser("save", help="persist a recorded suite from a YAML file")
    psave.add_argument("file", help="path to a suite YAML to validate, verify, and save")
    psave.add_argument("--no-replay", action="store_true")
    psave.add_argument("--server", action="append", default=[])

    sub.add_parser("serve", help="run as the FastMCP stdio server (same as no args)")
    return p


def main() -> None:
    # No subcommand -> behave exactly like the template: run as an MCP server.
    if len(sys.argv) == 1:
        mcp.run()
        return

    args = _build_parser().parse_args()
    if args.cmd == "serve":
        mcp.run()
        return

    from . import runner

    sys.exit(runner.cli_dispatch(args))
