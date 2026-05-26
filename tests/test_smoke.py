from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_tester_plugin import runner
from mcp_tester_plugin.server import ping, run_suite


def test_ping():
    assert ping() == "pong"


# --------------------------------------------------------------------------
# Smoke: run_suite tool must not crash when called from a running event loop
# --------------------------------------------------------------------------
_MINIMAL_SUITE_DOC: dict[str, Any] = {
    "schema": 1,
    "suite": "smoke / run-suite",
    "servers": {"s": {"plugin": "fake"}},
    "steps": [
        {"id": "step1", "server": "s", "tool": "fake_tool"},
    ],
}

_STUB_PASS_REPORT: dict[str, Any] = {
    "suite": "smoke / run-suite",
    "run_id": "e2e-stub",
    "result": "pass",
    "counts": {"steps": 1, "passed": 1, "failed": 0, "teardown_warnings": 0},
    "servers": [],
    "steps": [],
    "regressions": [],
    "teardown_warnings": [],
    "started_at": "2024-01-01T00:00:00Z",
    "duration_ms": 0,
    "policy": "continue",
}


@pytest.mark.anyio
async def test_run_suite_tool_no_crash(monkeypatch, tmp_path):
    """Calling the run_suite MCP tool from within a running event loop must not crash.

    This is the regression test for the 'Already running asyncio in this thread'
    error. The sync runner.run() would call anyio.run() inside FastMCP's loop and
    crash; run_suite now delegates to runner.run_async() which awaits _replay()
    directly.
    """
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return dict(_STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)

    sdir = tmp_path / "mcp-suites"
    sdir.mkdir()
    suite_path = sdir / "smoke__run-suite.yaml"
    suite_path.write_text(
        yaml.safe_dump(_MINIMAL_SUITE_DOC, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = await run_suite("smoke__run-suite")

    assert isinstance(result, dict)
    assert result["result"] == "pass"
    assert "suite_file" in result
