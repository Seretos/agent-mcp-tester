"""Unit tests for the deterministic runner's pure logic (no MCP, no network)."""

import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from mcp_tester_plugin import assertions, runner, suites


# --------------------------------------------------------------------------
# Templating
# --------------------------------------------------------------------------
def test_substitute_single_ref_preserves_type():
    assert assertions.substitute("${ticket_id}", {"ticket_id": 1234}) == 1234


def test_substitute_embedded_stringifies():
    out = assertions.substitute("ticket ${RUN_ID}", {"RUN_ID": "e2e-1-ab"})
    assert out == "ticket e2e-1-ab"


def test_substitute_recurses_dict_and_list():
    out = assertions.substitute(
        {"title": "t ${RUN_ID}", "ids": ["${a}", "x"]},
        {"RUN_ID": "r", "a": 7},
    )
    assert out == {"title": "t r", "ids": [7, "x"]}


def test_substitute_env(monkeypatch):
    monkeypatch.setenv("MCP_TESTER_FAKE_TOKEN", "secret")
    assert assertions.substitute("${env:MCP_TESTER_FAKE_TOKEN}", {}) == "secret"


def test_substitute_unresolved_raises():
    with pytest.raises(assertions.UnresolvedVariable):
        assertions.substitute("${nope}", {})


def test_find_references():
    refs = assertions.find_references({"a": "${x}", "b": ["${y}", "z ${x}"]})
    assert refs == {"x", "y"}


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------
DATA = {
    "id": 42,
    "title": "hello",
    "status": "open",
    "url": "https://example.test/42",
    "labels": ["bug", "e2e-test"],
    "nested": {"count": 3},
}


@pytest.mark.parametrize(
    "assertion,ok",
    [
        ({"path": "$.id", "op": "exists"}, True),
        ({"path": "$.missing", "op": "exists"}, False),
        ({"path": "$.missing", "op": "absent"}, True),
        ({"path": "$.title", "op": "equals", "value": "hello"}, True),
        ({"path": "$.title", "op": "equals", "value": "nope"}, False),
        ({"path": "$.status", "op": "in", "value": ["open", "todo"]}, True),
        ({"path": "$.url", "op": "matches", "value": "^https://"}, True),
        ({"path": "$.id", "op": "type", "value": "number"}, True),
        ({"path": "$.title", "op": "type", "value": "number"}, False),
        ({"path": "$.labels", "op": "contains", "value": "e2e-test"}, True),
        ({"path": "$.labels", "op": "length", "value": 2}, True),
        ({"path": "$.nested.count", "op": "ge", "value": 3}, True),
        ({"path": "$.nested.count", "op": "gt", "value": 3}, False),
    ],
)
def test_evaluate(assertion, ok):
    assert assertions.evaluate(assertion, DATA)["ok"] is ok


def test_extract_one_nested():
    found, value = assertions.extract_one("$.nested.count", DATA)
    assert found and value == 3


# --------------------------------------------------------------------------
# Suite validation
# --------------------------------------------------------------------------
GOOD_SUITE = {
    "schema": 1,
    "suite": "agent-project-issues / ticket-lifecycle",
    "servers": {"pi": {"plugin": "agent-project-issues", "server": "project-issues"}},
    "steps": [
        {
            "id": "create",
            "server": "pi",
            "tool": "create_ticket",
            "args": {"title": "t ${RUN_ID}"},
            "expect": [{"path": "$.id", "op": "exists"}],
            "capture": {"ticket_id": "$.id"},
        },
        {
            "id": "read",
            "server": "pi",
            "tool": "get_ticket",
            "args": {"id": "${ticket_id}"},
            "expect": [{"path": "$.id", "op": "equals", "value": "${ticket_id}"}],
        },
    ],
    "teardown": [
        {
            "id": "close",
            "server": "pi",
            "tool": "update_ticket",
            "args": {"id": "${ticket_id}", "status": "closed"},
            "on_missing_var": "skip",
        }
    ],
}


def test_validate_accepts_good_suite():
    suites.validate(GOOD_SUITE)  # must not raise


def test_validate_rejects_missing_steps():
    bad = dict(GOOD_SUITE)
    bad["steps"] = []
    with pytest.raises(suites.SuiteError):
        suites.validate(bad)


def test_validate_rejects_unknown_op():
    bad = {
        **GOOD_SUITE,
        "steps": [
            {
                "id": "x",
                "server": "pi",
                "tool": "t",
                "expect": [{"path": "$.id", "op": "frobnicate"}],
            }
        ],
    }
    with pytest.raises(suites.SuiteError):
        suites.validate(bad)


def test_validate_rejects_unknown_server():
    bad = {
        **GOOD_SUITE,
        "steps": [{"id": "x", "server": "ghost", "tool": "t"}],
    }
    with pytest.raises(suites.SuiteError):
        suites.validate(bad)


def test_validate_rejects_near_miss_assertion_key_assertions():
    bad = {
        **GOOD_SUITE,
        "steps": [
            {
                "id": "x",
                "server": "pi",
                "tool": "t",
                "assertions": [{"path": "$.id", "op": "exists"}],
            }
        ],
    }
    with pytest.raises(suites.SuiteError) as exc_info:
        suites.validate(bad)
    assert "expect" in str(exc_info.value)


def test_validate_rejects_near_miss_assertion_key_assert():
    bad = {
        **GOOD_SUITE,
        "steps": [
            {
                "id": "x",
                "server": "pi",
                "tool": "t",
                "assert": [{"path": "$.id", "op": "exists"}],
            }
        ],
    }
    with pytest.raises(suites.SuiteError) as exc_info:
        suites.validate(bad)
    assert "expect" in str(exc_info.value)


def test_default_filename():
    assert (
        suites.default_filename(GOOD_SUITE)
        == "agent-project-issues__ticket-lifecycle.yaml"
    )


def test_dataflow_warns_on_use_before_capture():
    suite = {
        "schema": 1,
        "suite": "x / y",
        "servers": {"s": {"plugin": "p"}},
        "steps": [
            {"id": "a", "server": "s", "tool": "t", "args": {"id": "${ghost}"}}
        ],
    }
    warnings = suites.dataflow_warnings(suite)
    assert any("ghost" in w for w in warnings)


def test_dataflow_clean_for_good_suite():
    assert suites.dataflow_warnings(GOOD_SUITE) == []


# --------------------------------------------------------------------------
# Minimal valid suite fixture for async tests
# --------------------------------------------------------------------------
MINIMAL_SUITE_DOC: dict[str, Any] = {
    "schema": 1,
    "suite": "test / minimal",
    "servers": {"s": {"plugin": "fake"}},
    "steps": [
        {"id": "step1", "server": "s", "tool": "fake_tool"},
    ],
}

STUB_PASS_REPORT: dict[str, Any] = {
    "suite": "test / minimal",
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

STUB_FAIL_REPORT: dict[str, Any] = {
    **STUB_PASS_REPORT,
    "result": "regression",
    "regressions": [{"id": "r1"}],
}


def _make_suite_file(tmp_path: Path) -> Path:
    """Write MINIMAL_SUITE_DOC into a temp mcp-suites dir and return its path."""
    sdir = tmp_path / "mcp-suites"
    sdir.mkdir()
    suite_path = sdir / "test__minimal.yaml"
    suite_path.write_text(
        yaml.safe_dump(MINIMAL_SUITE_DOC, sort_keys=False), encoding="utf-8"
    )
    return suite_path


# --------------------------------------------------------------------------
# Regression: sync run() called from within a running event loop returns an
# error dict (ticket #19: structured errors instead of raw crashes).
# --------------------------------------------------------------------------
@pytest.mark.anyio
async def test_run_from_running_loop_returns_error_dict(monkeypatch, tmp_path):
    """Calling the SYNC runner.run() from a running event loop now returns a
    structured error dict instead of propagating the RuntimeError.

    anyio.run() raises RuntimeError when called from within an already-running
    loop; after ticket #19 the except-clause in run() catches that and returns
    a dict with result='error' so the MCP/CLI caller always gets a well-formed
    report. The async entry points (run_async) remain the correct path for
    in-loop callers.
    """
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return STUB_PASS_REPORT

    monkeypatch.setattr(runner, "_replay", stub_replay)
    _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = runner.run("test__minimal")

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("result") == "error", f"Expected result='error', got: {result}"
    assert "error" in result, f"Expected 'error' key, got: {result}"
    assert "RuntimeError" in result["error"], (
        f"Expected 'RuntimeError' in error string, got: {result['error']!r}"
    )


# --------------------------------------------------------------------------
# Async entry points: happy-path and edge-cases (all stub _replay, no network)
# --------------------------------------------------------------------------
@pytest.mark.anyio
async def test_run_async_returns_stub_report(monkeypatch, tmp_path):
    """run_async awaits _replay and returns the report enriched with suite_file."""
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return dict(STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = await runner.run_async("test__minimal")

    assert result["result"] == "pass"
    assert "suite_file" in result
    assert "test__minimal" in result["suite_file"]


@pytest.mark.anyio
async def test_validate_suite_async_no_replay(monkeypatch, tmp_path):
    """validate_suite_async with verify_replay=False parses the file and returns
    valid=True without ever calling _replay."""
    replay_called = []

    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        replay_called.append(True)
        return dict(STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = await runner.validate_suite_async("test__minimal", verify_replay=False)

    assert result["valid"] is True
    assert "suite_file" in result
    assert replay_called == [], "_replay must not be called when verify_replay=False"


@pytest.mark.anyio
async def test_validate_suite_async_with_replay(monkeypatch, tmp_path):
    """validate_suite_async with verify_replay=True calls _replay and reflects result."""
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return dict(STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = await runner.validate_suite_async("test__minimal", verify_replay=True)

    assert result["valid"] is True
    assert "verify_replay" in result
    assert result["verify_replay"]["result"] == "pass"


@pytest.mark.anyio
async def test_save_suite_async_no_replay(monkeypatch, tmp_path):
    """save_suite_async with verify_replay=False writes the file without calling _replay."""
    replay_called = []

    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        replay_called.append(True)
        return dict(STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    # Ensure mcp-suites dir exists so save() can write to it
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)

    suite_yaml = yaml.safe_dump(MINIMAL_SUITE_DOC, sort_keys=False)
    result = await runner.save_suite_async(suite_yaml, verify_replay=False)

    assert result["saved"] is True
    assert "path" in result
    assert Path(result["path"]).is_file()
    assert replay_called == [], "_replay must not be called when verify_replay=False"


@pytest.mark.anyio
async def test_save_suite_async_replay_fails(monkeypatch, tmp_path):
    """save_suite_async does NOT write the file when replay returns a non-pass result."""
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return dict(STUB_FAIL_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)

    suite_yaml = yaml.safe_dump(MINIMAL_SUITE_DOC, sort_keys=False)
    result = await runner.save_suite_async(suite_yaml, verify_replay=True)

    assert result["saved"] is False
    assert "error" in result
    # No file should have been written
    saved_files = list((tmp_path / "mcp-suites").glob("*.yaml"))
    assert saved_files == []


@pytest.mark.anyio
async def test_save_suite_async_replay_passes(monkeypatch, tmp_path):
    """save_suite_async writes the file when replay returns a pass result."""
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return dict(STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)

    suite_yaml = yaml.safe_dump(MINIMAL_SUITE_DOC, sort_keys=False)
    result = await runner.save_suite_async(suite_yaml, verify_replay=True)

    assert result["saved"] is True
    assert "path" in result
    assert Path(result["path"]).is_file()


# --------------------------------------------------------------------------
# validate_suite_async: inline-YAML input path (regression + edge cases)
# --------------------------------------------------------------------------
MINIMAL_SUITE_YAML = yaml.safe_dump(MINIMAL_SUITE_DOC, sort_keys=False)


@pytest.mark.anyio
async def test_validate_suite_async_inline_yaml_no_replay(monkeypatch, tmp_path):
    """Regression test: passing raw YAML text must succeed (was: SuiteError no suite matching).

    Uses a tmp_path with NO mcp-suites/ dir so the file-path branch cannot resolve,
    forcing the inline-YAML branch. Asserts valid=True, inline=True, no suite_file,
    and _replay is never called.
    """
    replay_called = []

    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        replay_called.append(True)
        return dict(STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    # Deliberately do NOT create mcp-suites/ so resolve_suite_path raises SuiteError.

    result = await runner.validate_suite_async(MINIMAL_SUITE_YAML, verify_replay=False)

    assert result["valid"] is True
    assert result.get("inline") is True
    assert "suite_file" not in result
    assert replay_called == [], "_replay must not be called when verify_replay=False"


@pytest.mark.anyio
async def test_validate_suite_async_inline_yaml_with_replay(monkeypatch, tmp_path):
    """Inline-YAML path with verify_replay=True calls _replay and reflects result."""
    replay_calls = []

    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        replay_calls.append(True)
        return dict(STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = await runner.validate_suite_async(MINIMAL_SUITE_YAML, verify_replay=True)

    assert result["valid"] is True
    assert "verify_replay" in result
    assert result["verify_replay"]["result"] == "pass"
    assert len(replay_calls) == 1, "_replay must be called exactly once"


@pytest.mark.anyio
async def test_validate_suite_async_inline_yaml_invalid_schema(monkeypatch, tmp_path):
    """A YAML mapping that fails suites.validate propagates SuiteError (not silent)."""
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    # schema: 99 with no suite/servers/steps — structurally invalid.
    bad_yaml = "schema: 99\n"

    with pytest.raises(suites.SuiteError) as exc_info:
        await runner.validate_suite_async(bad_yaml, verify_replay=False)
    assert "schema: 1" in str(exc_info.value)


@pytest.mark.anyio
async def test_validate_suite_async_inline_not_a_mapping(monkeypatch, tmp_path):
    """A YAML string that parses to a list (not a mapping) must raise SuiteError."""
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    not_a_mapping = "- a\n- b\n"

    with pytest.raises(suites.SuiteError):
        await runner.validate_suite_async(not_a_mapping, verify_replay=False)


@pytest.mark.anyio
async def test_validate_suite_async_file_path_still_works(monkeypatch, tmp_path):
    """File-path branch is not broken: a saved-suite name resolves to suite_file, no inline key."""
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return dict(STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = await runner.validate_suite_async("test__minimal", verify_replay=False)

    assert result["valid"] is True
    assert "suite_file" in result
    assert "inline" not in result


@pytest.mark.anyio
async def test_validate_suite_async_invalid_file_schema_error_propagates(monkeypatch, tmp_path):
    """Regression: a suite file that exists but fails schema validation must raise a schema
    SuiteError — NOT the generic 'not a file path / not a YAML mapping' fallback.

    The narrowed try/except (only around resolve_suite_path) ensures suites.load's
    SuiteError propagates directly instead of being swallowed and retried as inline YAML.
    """
    sdir = tmp_path / "mcp-suites"
    sdir.mkdir()
    # Write a structurally invalid suite (wrong schema version, no suite/steps).
    invalid_doc = {"schema": 99, "note": "this is invalid"}
    invalid_file = sdir / "invalid-suite.yaml"
    invalid_file.write_text(
        yaml.safe_dump(invalid_doc, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    with pytest.raises(suites.SuiteError) as exc_info:
        await runner.validate_suite_async("invalid-suite", verify_replay=False)

    # The error must reflect the schema problem, not the generic inline-YAML fallback message.
    error_msg = str(exc_info.value)
    assert "not a file path" not in error_msg, (
        f"Got fallback message instead of schema error: {error_msg!r}"
    )
    assert "not a YAML mapping" not in error_msg, (
        f"Got fallback message instead of schema error: {error_msg!r}"
    )


# --------------------------------------------------------------------------
# resolve_suite_path: suite-field resolution (Finding B)
# --------------------------------------------------------------------------

SUITE_FIELD_DOC: dict[str, Any] = {
    "schema": 1,
    "suite": "agent-worktree / worktree-lifecycle",
    "servers": {"aw": {"plugin": "agent-worktree"}},
    "steps": [
        {"id": "step1", "server": "aw", "tool": "list_worktrees"},
    ],
}


def _make_suite_field_file(tmp_path: Path) -> Path:
    """Write SUITE_FIELD_DOC into a temp mcp-suites dir and return its path."""
    sdir = tmp_path / "mcp-suites"
    sdir.mkdir(exist_ok=True)
    suite_path = sdir / "agent-worktree__worktree-lifecycle.yaml"
    suite_path.write_text(
        yaml.safe_dump(SUITE_FIELD_DOC, sort_keys=False), encoding="utf-8"
    )
    return suite_path


def test_resolve_suite_path_by_suite_field(tmp_path):
    """Regression: resolve_suite_path must resolve by the human-readable suite: field value."""
    suite_path = _make_suite_field_file(tmp_path)

    # Temporarily set the env var using os.environ directly (pure function test).
    orig = os.environ.copy()
    try:
        os.environ["MCP_TESTER_ROOT"] = str(tmp_path)
        result = suites.resolve_suite_path("agent-worktree / worktree-lifecycle")
        assert result.is_file()
        assert result == suite_path.resolve()
    finally:
        # Restore env to avoid polluting other tests.
        for k in list(os.environ):
            if k not in orig:
                del os.environ[k]
        os.environ.update(orig)


def test_resolve_suite_path_filename_still_wins(tmp_path, monkeypatch):
    """Filename-stem resolution must still work after the suite-field scan is added."""
    _make_suite_field_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = suites.resolve_suite_path("agent-worktree__worktree-lifecycle")
    assert result.is_file()
    assert result.name == "agent-worktree__worktree-lifecycle.yaml"


def test_resolve_suite_path_no_match_raises(tmp_path, monkeypatch):
    """An unknown name raises SuiteError whether or not a suites dir exists."""
    sdir = tmp_path / "mcp-suites"
    sdir.mkdir()
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    with pytest.raises(suites.SuiteError, match="no suite matching"):
        suites.resolve_suite_path("completely-unknown / suite-name")


def test_resolve_suite_path_skips_corrupt_file(tmp_path, monkeypatch):
    """A corrupt YAML file alongside a valid one must not prevent resolution by suite: field."""
    sdir = tmp_path / "mcp-suites"
    sdir.mkdir()
    # Corrupt file that will fail yaml.safe_load.
    (sdir / "corrupt.yaml").write_text("key: [\nunot closed", encoding="utf-8")
    # Valid file with the target suite: field.
    valid_path = sdir / "valid-suite.yaml"
    valid_path.write_text(
        yaml.safe_dump(SUITE_FIELD_DOC, sort_keys=False), encoding="utf-8"
    )
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = suites.resolve_suite_path("agent-worktree / worktree-lifecycle")
    assert result == valid_path.resolve()


def test_resolve_suite_path_skips_targets_yaml(tmp_path, monkeypatch):
    """targets.yaml must not be scanned even if it contains a suite-like key."""
    sdir = tmp_path / "mcp-suites"
    sdir.mkdir()
    # Put a targets.yaml that happens to have a "suite" key to be extra sure.
    targets = sdir / "targets.yaml"
    targets.write_text(
        yaml.safe_dump({"suite": "agent-worktree / worktree-lifecycle", "servers": {}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    # Without any valid suite file the lookup must fail (targets.yaml was skipped).
    with pytest.raises(suites.SuiteError, match="no suite matching"):
        suites.resolve_suite_path("agent-worktree / worktree-lifecycle")


@pytest.mark.anyio
async def test_run_async_resolves_by_suite_field(monkeypatch, tmp_path):
    """run_async must accept the human-readable suite: field value as the suite identifier."""
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return dict(STUB_PASS_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    _make_suite_field_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = await runner.run_async("agent-worktree / worktree-lifecycle")

    assert result["result"] == "pass"
    assert "suite_file" in result
    assert "worktree-lifecycle" in result["suite_file"]


# --------------------------------------------------------------------------
# _strip_tool_prefix unit tests
# --------------------------------------------------------------------------

def test_strip_prefix_removes_standard_prefix():
    """Full Claude Code harness prefix is stripped, leaving the bare tool name."""
    name = "mcp__plugin_agent-project-issues_project-issues__list_ticket_statuses"
    assert runner._strip_tool_prefix(name) == "list_ticket_statuses"


def test_strip_prefix_bare_name_unchanged():
    """A bare name (no prefix) passes through unchanged."""
    assert runner._strip_tool_prefix("list_ticket_statuses") == "list_ticket_statuses"


def test_strip_prefix_single_segment_plugin():
    """A single-segment plugin prefix is stripped correctly."""
    assert runner._strip_tool_prefix("mcp__plugin_my-plugin__do_thing") == "do_thing"


def test_strip_prefix_empty_string_unchanged():
    """An empty string passes through unchanged (no error)."""
    assert runner._strip_tool_prefix("") == ""


def test_strip_prefix_partial_match_unchanged():
    """A string starting with mcp__ but lacking the closing __ is returned unchanged."""
    # Has mcp__ prefix but no closing __ after the plugin segment.
    partial = "mcp__plugin_my-plugin_do_thing"
    result = runner._strip_tool_prefix(partial)
    assert result == partial


def test_strip_prefix_non_plugin_mcp_name_unchanged():
    """A tool name that looks like an MCP tool name but lacks the 'plugin_' anchor
    must NOT be stripped — the regex is now anchored to mcp__plugin_."""
    name = "mcp__admin__reset_cache"
    assert runner._strip_tool_prefix(name) == "mcp__admin__reset_cache"


# --------------------------------------------------------------------------
# _exec_step regression and edge tests (mock ClientSession, no real MCP)
# --------------------------------------------------------------------------

class _FakeCallResult:
    """Minimal stand-in for an MCP CallToolResult."""
    def __init__(self, text: str = "", is_error: bool = False):
        from mcp.types import TextContent
        self.content = [TextContent(type="text", text=text)]
        self.isError = is_error
        self.structuredContent = None


class _FakeSession:
    """Duck-typed ClientSession that records which tool was called."""
    def __init__(self, result_text: str = '{"ok": true}'):
        self._result_text = result_text
        self.called_with: list[tuple[str, dict]] = []

    async def call_tool(self, tool: str, arguments: dict) -> _FakeCallResult:
        self.called_with.append((tool, arguments))
        return _FakeCallResult(text=self._result_text)


@pytest.mark.anyio
async def test_exec_step_prefixed_tool_name_matches_bare_toolnames():
    """Regression: a step whose tool: is a harness-prefixed name must succeed
    when the server advertises only the bare name."""
    session = _FakeSession()
    sessions = {"s": (session, {"my_tool"}, "fake-mcp")}
    step = {
        "id": "step1",
        "server": "s",
        "tool": "mcp__plugin_x_y__my_tool",
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "pass", f"expected pass, got: {result}"
    assert regressions == [], f"unexpected regressions: {regressions}"
    # The bare name is what was actually called.
    assert session.called_with == [("my_tool", {})]
    # The output tool field is also the bare name.
    assert result["tool"] == "my_tool"


@pytest.mark.anyio
async def test_exec_step_bare_tool_name_still_matches():
    """Bare tool names (no prefix) continue to work after the prefix-stripping change."""
    session = _FakeSession()
    sessions = {"s": (session, {"my_tool"}, "fake-mcp")}
    step = {
        "id": "step1",
        "server": "s",
        "tool": "my_tool",
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "pass", f"expected pass, got: {result}"
    assert regressions == []


@pytest.mark.anyio
async def test_exec_step_unknown_tool_still_fails():
    """A tool name that is neither bare nor prefixed and is absent from toolnames
    must still produce status=fail with class=contract."""
    session = _FakeSession()
    sessions = {"s": (session, {"my_tool"}, "fake-mcp")}
    step = {
        "id": "step1",
        "server": "s",
        "tool": "completely_unknown_tool",
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "fail"
    assert len(regressions) == 1
    assert regressions[0]["class"] == "contract"


# --------------------------------------------------------------------------
# dataflow_warnings: prefixed tool name warning
# --------------------------------------------------------------------------

def test_dataflow_warns_on_prefixed_tool_name():
    """A suite step with a harness-prefixed tool: name must produce a warning
    mentioning the step id or the word 'prefix'."""
    suite = {
        "schema": 1,
        "suite": "x / y",
        "servers": {"s": {"plugin": "p"}},
        "steps": [
            {
                "id": "step_prefixed",
                "server": "s",
                "tool": "mcp__plugin_x__do_thing",
            }
        ],
    }
    warnings = suites.dataflow_warnings(suite)
    assert warnings, "expected at least one warning for a prefixed tool name"
    assert any(
        "step_prefixed" in w or "prefix" in w for w in warnings
    ), f"expected warning mentioning step id or 'prefix', got: {warnings}"


# --------------------------------------------------------------------------
# Non-string tool: field robustness (fix for ticket #10 blocking finding)
# --------------------------------------------------------------------------

def test_strip_prefix_non_string_returned_unchanged():
    """_strip_tool_prefix with a non-string input (e.g. integer 123) must return
    the value unchanged without raising TypeError."""
    result = runner._strip_tool_prefix(123)
    assert result == 123


@pytest.mark.anyio
async def test_exec_step_non_string_tool_fails_gracefully():
    """A step with tool: 123 (truthy non-string) must return status=fail with
    class=contract and must NOT raise TypeError."""
    session = _FakeSession()
    sessions = {"s": (session, {"real_tool"}, "fake-mcp")}
    step = {
        "id": "step_bad_tool",
        "server": "s",
        "tool": 123,
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "fail", f"expected fail, got: {result}"
    assert len(regressions) == 1, f"expected one regression, got: {regressions}"
    assert regressions[0]["class"] == "contract", (
        f"expected class=contract, got: {regressions[0]['class']!r}"
    )


def test_dataflow_no_crash_on_non_string_tool():
    """dataflow_warnings on a suite whose step has tool: 123 must return a list
    (possibly empty) without raising TypeError."""
    suite = {
        "schema": 1,
        "suite": "x / y",
        "servers": {"s": {"plugin": "p"}},
        "steps": [
            {
                "id": "bad_tool_step",
                "server": "s",
                "tool": 123,
            }
        ],
    }
    result = suites.dataflow_warnings(suite)
    assert isinstance(result, list)


# --------------------------------------------------------------------------
# Ticket #12: validate_suite UX fixes — schema error message & valid semantics
# --------------------------------------------------------------------------

def test_suite_validate_missing_schema_error_message():
    """suites.validate({suite: x}) with no schema field must raise SuiteError
    whose message contains the literal string 'schema: 1'."""
    with pytest.raises(suites.SuiteError) as exc_info:
        suites.validate({"suite": "x"})
    assert "schema: 1" in str(exc_info.value), (
        f"Expected 'schema: 1' in error message, got: {exc_info.value!r}"
    )


def test_suite_validate_wrong_schema_version_error_message():
    """suites.validate with schema: 99 must raise SuiteError whose message
    contains both 'schema: 1' and the offending value '99'."""
    with pytest.raises(suites.SuiteError) as exc_info:
        suites.validate({"schema": 99})
    msg = str(exc_info.value)
    assert "schema: 1" in msg, (
        f"Expected 'schema: 1' in error message, got: {msg!r}"
    )
    assert "99" in msg, (
        f"Expected offending value '99' in error message, got: {msg!r}"
    )


@pytest.mark.anyio
async def test_validate_suite_async_valid_survives_replay_failure(monkeypatch, tmp_path):
    """Regression: valid=True must survive when replay fails.

    A structurally valid suite must return valid=True even when the replay
    returns result='regression'. The replay outcome lives exclusively under
    the verify_replay key and does not overwrite the schema-validity signal.
    """
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return dict(STUB_FAIL_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = await runner.validate_suite_async("test__minimal", verify_replay=True)

    assert result["valid"] is True, (
        f"valid must be True for a structurally valid suite, got: {result['valid']!r}"
    )
    assert "verify_replay" in result
    assert result["verify_replay"]["result"] == "regression", (
        f"replay result must be 'regression', got: {result['verify_replay']['result']!r}"
    )


@pytest.mark.anyio
async def test_validate_suite_async_inline_valid_survives_replay_failure(monkeypatch, tmp_path):
    """Regression (inline-YAML branch): valid=True must survive when replay fails.

    Same invariant as the file-path variant but exercises the inline-YAML
    code path (no mcp-suites/ dir so resolve_suite_path raises SuiteError).
    """
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return dict(STUB_FAIL_REPORT)

    monkeypatch.setattr(runner, "_replay", stub_replay)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    # Deliberately do NOT create mcp-suites/ so the inline branch is taken.

    result = await runner.validate_suite_async(MINIMAL_SUITE_YAML, verify_replay=True)

    assert result["valid"] is True, (
        f"valid must be True for a structurally valid suite (inline), got: {result['valid']!r}"
    )
    assert result.get("inline") is True
    assert "verify_replay" in result
    assert result["verify_replay"]["result"] == "regression", (
        f"replay result must be 'regression', got: {result['verify_replay']['result']!r}"
    )


def test_sync_validate_suite_valid_survives_replay_failure(monkeypatch, tmp_path):
    """Sync validate_suite: valid=True must survive when replay fails.

    Exercises the sync twin of the async function. Uses monkeypatch to stub
    anyio.run so no event loop is needed.
    """
    monkeypatch.setattr(runner, "_replay", None)  # replaced by anyio.run stub below

    def fake_anyio_run(coro_func, *args, **kwargs):
        # anyio.run(coro_func, ...) — return the fail report
        return dict(STUB_FAIL_REPORT)

    import anyio as _anyio
    monkeypatch.setattr(_anyio, "run", fake_anyio_run)

    _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = runner.validate_suite("test__minimal", verify_replay=True)

    assert result["valid"] is True, (
        f"valid must be True for a structurally valid suite (sync), got: {result['valid']!r}"
    )
    assert "verify_replay" in result
    assert result["verify_replay"]["result"] == "regression"


# --------------------------------------------------------------------------
# Ticket #12: cli_dispatch validate exit-code honours replay result
# --------------------------------------------------------------------------

import argparse as _argparse


def test_cli_dispatch_validate_exits_nonzero_on_replay_failure(monkeypatch):
    """cli_dispatch validate must return 1 when the suite is structurally valid
    but the verify-replay result is not 'pass' (e.g. 'regression').

    This pins the behavioral fix: previously the exit code was determined solely
    by out.get("valid"), so a valid suite with a failing replay would exit 0.
    The fixed logic also checks verify_replay.result and exits 1 on any non-pass
    replay outcome.
    """
    stub_out = {"valid": True, "verify_replay": {"result": "regression"}}

    def fake_validate_suite(suite, *, verify_replay, overrides):
        return stub_out

    monkeypatch.setattr(runner, "validate_suite", fake_validate_suite)

    args = _argparse.Namespace(
        cmd="validate",
        suite="test__minimal",
        no_replay=False,
        server=[],
    )
    exit_code = runner.cli_dispatch(args)

    assert exit_code == 1, (
        f"Expected exit code 1 for valid=True + replay result='regression', got {exit_code}"
    )


def test_cli_dispatch_validate_exits_zero_on_replay_pass(monkeypatch):
    """cli_dispatch validate must return 0 when the suite is structurally valid
    and the verify-replay result is 'pass'.
    """
    stub_out = {"valid": True, "verify_replay": {"result": "pass"}}

    def fake_validate_suite(suite, *, verify_replay, overrides):
        return stub_out

    monkeypatch.setattr(runner, "validate_suite", fake_validate_suite)

    args = _argparse.Namespace(
        cmd="validate",
        suite="test__minimal",
        no_replay=False,
        server=[],
    )
    exit_code = runner.cli_dispatch(args)

    assert exit_code == 0, (
        f"Expected exit code 0 for valid=True + replay result='pass', got {exit_code}"
    )


def test_cli_dispatch_validate_exits_zero_no_verify_replay(monkeypatch):
    """cli_dispatch validate must return 0 when the suite is valid and verify_replay
    is absent from the output (--no-replay flag: no replay was run).

    When verify_replay is missing, out.get("verify_replay", {}).get("result", "pass")
    defaults to "pass", so exit code must be 0.
    """
    stub_out = {"valid": True}

    def fake_validate_suite(suite, *, verify_replay, overrides):
        return stub_out

    monkeypatch.setattr(runner, "validate_suite", fake_validate_suite)

    args = _argparse.Namespace(
        cmd="validate",
        suite="test__minimal",
        no_replay=True,
        server=[],
    )
    exit_code = runner.cli_dispatch(args)

    assert exit_code == 0, (
        f"Expected exit code 0 for valid=True + no verify_replay key, got {exit_code}"
    )


# --------------------------------------------------------------------------
# Ticket #16: _normalize_placeholders — double-brace placeholder normalisation
# --------------------------------------------------------------------------

def test_normalize_placeholders_double_brace_expanded():
    """``{{run_id}}`` and ``{{ticket_id}}`` must be expanded to ``${run_id}`` /
    ``${ticket_id}`` in strings, dicts, and lists. Surrounding whitespace is
    stripped but the original case of the placeholder name is preserved — capture
    keys are stored verbatim in ``variables`` so lowercasing would break lookup."""
    # Plain string
    assert runner._normalize_placeholders("{{run_id}}") == "${run_id}"
    assert runner._normalize_placeholders("{{ticket_id}}") == "${ticket_id}"
    # Mixed-case capture variable name → case preserved (NOT lowercased)
    assert runner._normalize_placeholders("{{ticketId}}") == "${ticketId}"
    # Uppercase name → case preserved
    assert runner._normalize_placeholders("{{RUN_ID}}") == "${RUN_ID}"
    # Whitespace around name → stripped, case preserved
    assert runner._normalize_placeholders("{{ Run_Id }}") == "${Run_Id}"
    # Embedded in a longer string
    assert runner._normalize_placeholders("title-{{run_id}}") == "title-${run_id}"
    # Dict values are recursed
    out_dict = runner._normalize_placeholders({"title": "wt-{{run_id}}", "other": 42})
    assert out_dict == {"title": "wt-${run_id}", "other": 42}
    # List items are recursed
    out_list = runner._normalize_placeholders(["{{run_id}}", "{{ticket_id}}", "plain"])
    assert out_list == ["${run_id}", "${ticket_id}", "plain"]


def test_normalize_placeholders_dollar_brace_unchanged():
    """Existing ``${var}`` strings and non-string scalars must pass through unchanged
    (idempotence: a string that is already normalised stays the same)."""
    # Already-dollar-brace strings are untouched (no ``{{...}}`` to match)
    assert runner._normalize_placeholders("${run_id}") == "${run_id}"
    assert runner._normalize_placeholders("title ${RUN_ID}") == "title ${RUN_ID}"
    # Non-string scalars pass through unchanged
    assert runner._normalize_placeholders(42) == 42
    assert runner._normalize_placeholders(True) is True
    assert runner._normalize_placeholders(None) is None
    # Idempotence: calling twice gives same result
    result = runner._normalize_placeholders("{{run_id}}")
    assert runner._normalize_placeholders(result) == result


# --------------------------------------------------------------------------
# Ticket #16: _exec_step with double-brace placeholders (after normalisation)
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_exec_step_double_brace_run_id_substituted():
    """``_exec_step`` with args containing ``{{run_id}}`` must dispatch
    the resolved value after ``_normalize_placeholders`` is applied first
    (mirroring the ``_replay`` flow which normalises before calling ``_exec_step``).
    """
    session = _FakeSession(result_text='{"ok": true}')
    sessions = {"s": (session, {"create_ticket"}, "fake-mcp")}
    raw_step = {
        "id": "create",
        "server": "s",
        "tool": "create_ticket",
        "args": {"title": "wt-{{run_id}}"},
    }
    # Normalise first, as _replay does.
    step = runner._normalize_placeholders(raw_step)
    variables = {"run_id": "e2e-test-1"}
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, variables, regressions,
                                     is_teardown=False)

    assert result["status"] == "pass", f"expected pass, got: {result}"
    assert regressions == [], f"unexpected regressions: {regressions}"
    # The resolved value must have been dispatched to the session.
    assert session.called_with == [("create_ticket", {"title": "wt-e2e-test-1"})]


@pytest.mark.anyio
async def test_exec_step_double_brace_assertion_value_substituted():
    """An assertion ``value`` containing ``{{run_id}}`` must be resolved (via
    normalisation → substitution) before comparison so an ``equals`` check against
    the real captured value passes instead of false-failing."""
    # The tool returns a result whose title matches the resolved run_id value.
    session = _FakeSession(result_text='{"title": "wt-e2e-test-1"}')
    sessions = {"s": (session, {"create_ticket"}, "fake-mcp")}
    raw_step = {
        "id": "create",
        "server": "s",
        "tool": "create_ticket",
        "args": {},
        "expect": [
            {"path": "$.title", "op": "equals", "value": "wt-{{run_id}}"}
        ],
    }
    # Normalise first, as _replay does.
    step = runner._normalize_placeholders(raw_step)
    variables = {"run_id": "e2e-test-1"}
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, variables, regressions,
                                     is_teardown=False)

    assert result["status"] == "pass", f"expected pass, got: {result}"
    # All assertions must be ok.
    assert all(a["ok"] for a in result.get("assertions", [])), (
        f"expected all assertions ok, got: {result.get('assertions')}"
    )


@pytest.mark.anyio
async def test_exec_step_double_brace_captured_var_in_next_step():
    """A first step captures ``ticket_id``; a later step uses ``{{ticket_id}}``
    in its args. After normalisation + substitution the later step receives the
    captured value rather than the literal ``{{ticket_id}}``."""
    # Step 1: capture ticket_id from the result.
    session1 = _FakeSession(result_text='{"id": 99, "_isError": false}')
    sessions = {"s": (session1, {"create_ticket", "get_ticket"}, "fake-mcp")}

    step1_raw = {
        "id": "create",
        "server": "s",
        "tool": "create_ticket",
        "args": {},
        "capture": {"ticket_id": "$.id"},
    }
    step1 = runner._normalize_placeholders(step1_raw)
    variables: dict[str, Any] = {"run_id": "e2e-test-2"}
    regressions: list[dict[str, Any]] = []

    res1 = await runner._exec_step(step1, sessions, variables, regressions,
                                   is_teardown=False)
    assert res1["status"] == "pass", f"step1 failed: {res1}"
    # Captured value must be in variables now.
    assert variables.get("ticket_id") == 99

    # Step 2: use {{ticket_id}} in args; normalise before calling _exec_step.
    step2_raw = {
        "id": "read",
        "server": "s",
        "tool": "get_ticket",
        "args": {"id": "{{ticket_id}}"},
    }
    step2 = runner._normalize_placeholders(step2_raw)
    # After normalisation, "{{ticket_id}}" → "${ticket_id}"
    assert step2["args"]["id"] == "${ticket_id}"

    res2 = await runner._exec_step(step2, sessions, variables, regressions,
                                   is_teardown=False)
    assert res2["status"] == "pass", f"step2 failed: {res2}"
    # The resolved integer must have been dispatched.
    assert session1.called_with[-1] == ("get_ticket", {"id": 99})


# --------------------------------------------------------------------------
# Ticket #16: run_suite MCP tool returns structured error dict on exception
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_run_suite_mcp_tool_returns_error_dict_on_exception(monkeypatch):
    """Monkeypatching ``runner.run_async`` to raise RuntimeError must result in
    ``run_suite`` returning a dict with ``result="error"`` and an ``error`` key
    rather than propagating the exception."""
    import importlib
    from mcp_tester_plugin import server

    # Import runner so we can monkeypatch it.
    from mcp_tester_plugin import runner as _runner

    async def boom(suite, *, policy, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(_runner, "run_async", boom)

    # Call the underlying function directly (bypass FastMCP's tool wrapper).
    # server.run_suite is the decorated async function — call it by name.
    result = await server.run_suite("test__minimal", policy="continue")

    assert isinstance(result, dict), f"expected dict, got {type(result)}: {result!r}"
    assert result.get("result") == "error", f"expected result='error', got: {result}"
    assert "error" in result, f"expected 'error' key in result, got: {result}"
    assert "RuntimeError" in result["error"], (
        f"expected 'RuntimeError' in error string, got: {result['error']!r}"
    )
    assert "boom" in result["error"], (
        f"expected 'boom' in error string, got: {result['error']!r}"
    )
    assert "regressions" in result, f"expected 'regressions' key, got: {result}"


# --------------------------------------------------------------------------
# Ticket #16 review fix B1: legacy ${RUN_ID} resolves in the real variables table
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_exec_step_legacy_RUN_ID_resolves():
    """Regression: a step using the legacy ``${RUN_ID}`` dollar-brace form must
    still resolve after normalisation because ``_replay`` binds BOTH ``run_id``
    and ``RUN_ID`` in the variables table.

    Reproduces the breakage: if only ``run_id`` is bound, ``${RUN_ID}`` raises
    ``UnresolvedVariable``, turning every pre-existing recorded suite into a fail.
    """
    session = _FakeSession(result_text='{"ok": true}')
    sessions = {"s": (session, {"create_ticket"}, "fake-mcp")}
    raw_step = {
        "id": "create",
        "server": "s",
        "tool": "create_ticket",
        # Legacy dollar-brace form: passes through _normalize_placeholders unchanged.
        "args": {"title": "wt-${RUN_ID}"},
    }
    # Normalise as _replay does — the dollar-brace form must be left intact.
    step = runner._normalize_placeholders(raw_step)
    assert step["args"]["title"] == "wt-${RUN_ID}", (
        "normalisation must not alter existing ${RUN_ID} references"
    )

    # Build the variables table exactly as _replay does (both keys bound).
    run_id = "e2e-test-legacy"
    variables: dict[str, Any] = {"run_id": run_id, "RUN_ID": run_id}
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, variables, regressions,
                                     is_teardown=False)

    assert result["status"] == "pass", (
        f"legacy ${{RUN_ID}} must resolve; got status={result['status']!r}, "
        f"error={result.get('error')!r}"
    )
    assert regressions == [], f"unexpected regressions: {regressions}"
    # The resolved value must have been dispatched.
    assert session.called_with == [("create_ticket", {"title": f"wt-{run_id}"})]


# --------------------------------------------------------------------------
# Ticket #16 review fix B2: {{env:MY_VAR}} preserves env-var name case
# --------------------------------------------------------------------------

def test_normalize_placeholders_env_var_case_preserved():
    """``{{env:MY_VAR}}`` must normalise to ``${env:MY_VAR}`` (uppercase preserved).

    Lowercasing the env-var name breaks lookup on Linux where ``os.environ``
    is case-sensitive: ``os.environ.get("my_var")`` misses ``MY_VAR``.
    """
    assert runner._normalize_placeholders("{{env:MY_VAR}}") == "${env:MY_VAR}"
    assert runner._normalize_placeholders("{{env:GITHUB_TOKEN}}") == "${env:GITHUB_TOKEN}"
    # Mixed case is also preserved.
    assert runner._normalize_placeholders("{{env:My_Var}}") == "${env:My_Var}"
    # Whitespace around the whole name is stripped, but var-name case is kept.
    assert runner._normalize_placeholders("{{ env:MY_VAR }}") == "${env:MY_VAR}"
    # Embedding in a longer string.
    assert runner._normalize_placeholders("token-{{env:API_TOKEN}}") == "token-${env:API_TOKEN}"


def test_normalize_placeholders_env_var_resolves_correctly(monkeypatch):
    """End-to-end: ``{{env:MY_VAR}}`` normalises and then resolves via
    ``assertions.substitute`` against the real environment (monkeypatched).

    Confirms both the normalisation (case-preserving) and the lookup path
    (``assertions._lookup`` uses ``os.environ.get(name[4:])``).
    """
    monkeypatch.setenv("MCP_TESTER_UPPER_TEST_VAR", "resolved_value")
    # Normalise the double-brace form.
    normalised = runner._normalize_placeholders("prefix-{{env:MCP_TESTER_UPPER_TEST_VAR}}")
    assert normalised == "prefix-${env:MCP_TESTER_UPPER_TEST_VAR}"
    # Substitute via the real assertions path (no variables dict needed for env refs).
    result = assertions.substitute(normalised, {})
    assert result == "prefix-resolved_value"


# --------------------------------------------------------------------------
# Ticket #16 review nit: error dict from run_suite has all human_summary keys
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_run_suite_error_dict_is_human_summary_safe(monkeypatch):
    """The error dict returned by ``run_suite`` on exception must contain at
    minimum the keys that ``report.human_summary`` reads: ``run_id``,
    ``counts``, ``servers``.  Missing keys cause a KeyError / AttributeError
    when the CLI's ``run`` command calls ``human_summary(rep)``.
    """
    from mcp_tester_plugin import report as _report
    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server

    async def boom(suite, *, policy, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(_runner, "run_async", boom)

    result = await server.run_suite("test__minimal", policy="continue")

    # human_summary must not raise.
    summary = _report.human_summary(result)
    assert isinstance(summary, str)
    # The minimal keys must be present.
    assert "run_id" in result
    assert "counts" in result
    assert "servers" in result


# --------------------------------------------------------------------------
# Ticket #16 review: dataflow_warnings does not warn on "run_id" reference
# --------------------------------------------------------------------------

def test_dataflow_no_warning_for_run_id_lowercase():
    """A suite step referencing ``${run_id}`` (the normalised double-brace form)
    must NOT produce a dataflow warning — the runner always binds this key."""
    suite = {
        "schema": 1,
        "suite": "x / y",
        "servers": {"s": {"plugin": "p"}},
        "steps": [
            {
                "id": "step1",
                "server": "s",
                "tool": "create_ticket",
                "args": {"title": "wt-${run_id}"},
            }
        ],
    }
    warnings = suites.dataflow_warnings(suite)
    assert not any("run_id" in w for w in warnings), (
        f"Expected no warning for ${{run_id}}, got: {warnings}"
    )


# --------------------------------------------------------------------------
# Ticket #16 re-review [blocking A]: _normalize_placeholders must NOT lowercase
# user-defined variable names — case is preserved for capture key lookups.
# --------------------------------------------------------------------------

def test_normalize_placeholders_mixed_case_capture_key_preserved():
    """Regression: ``{{ticketId}}`` must normalise to ``${ticketId}`` (not ``${ticketid}``).

    Capture keys are stored verbatim in ``variables`` (e.g.
    ``capture: {ticketId: "$.id"}`` stores ``variables["ticketId"]``).  If
    normalisation lowercases the name, a later ``{{ticketId}}`` would become
    ``${ticketid}`` which finds no binding and raises ``UnresolvedVariable``.
    """
    assert runner._normalize_placeholders("{{ticketId}}") == "${ticketId}"
    assert runner._normalize_placeholders("{{MyCapture}}") == "${MyCapture}"
    assert runner._normalize_placeholders("{{TICKET_ID}}") == "${TICKET_ID}"
    # Whitespace stripped but case preserved.
    assert runner._normalize_placeholders("{{ TicketId }}") == "${TicketId}"


@pytest.mark.anyio
async def test_exec_step_mixed_case_capture_key_resolves():
    """Regression: a step that captures into a mixed-case key (``ticketId``) and a
    later step using ``{{ticketId}}`` must succeed end-to-end after normalisation.

    Before the fix, normalisation lowercased ``ticketId`` → ``ticketid``, but
    ``variables`` held ``ticketId``, causing ``UnresolvedVariable``.
    """
    session = _FakeSession(result_text='{"id": 42, "_isError": false}')
    sessions = {"s": (session, {"create_ticket", "get_ticket"}, "fake-mcp")}

    # Step 1: capture into mixed-case key.
    step1_raw = {
        "id": "create",
        "server": "s",
        "tool": "create_ticket",
        "args": {},
        "capture": {"ticketId": "$.id"},
    }
    step1 = runner._normalize_placeholders(step1_raw)
    variables: dict[str, Any] = {"run_id": "r1", "RUN_ID": "r1"}
    regressions: list[dict[str, Any]] = []

    res1 = await runner._exec_step(step1, sessions, variables, regressions, is_teardown=False)
    assert res1["status"] == "pass", f"step1 failed: {res1}"
    assert variables.get("ticketId") == 42, "ticketId must be captured verbatim"

    # Step 2: reference via double-brace with the same mixed case.
    step2_raw = {
        "id": "read",
        "server": "s",
        "tool": "get_ticket",
        "args": {"id": "{{ticketId}}"},  # must normalise to ${ticketId}, NOT ${ticketid}
    }
    step2 = runner._normalize_placeholders(step2_raw)
    assert step2["args"]["id"] == "${ticketId}", (
        f"normalisation must preserve case: got {step2['args']['id']!r}"
    )

    res2 = await runner._exec_step(step2, sessions, variables, regressions, is_teardown=False)
    assert res2["status"] == "pass", (
        f"step2 failed (unresolved variable?): status={res2['status']!r}, "
        f"error={res2.get('error')!r}"
    )
    assert session.called_with[-1] == ("get_ticket", {"id": 42})


# --------------------------------------------------------------------------
# Ticket #16 re-review [blocking B]: dataflow_warnings detects {{...}} placeholders
# in raw (pre-normalisation) docs — use-before-capture must not be silently missed.
# --------------------------------------------------------------------------

def test_dataflow_warns_on_double_brace_use_before_capture():
    """Regression: a suite using ``{{ticket_id}}`` BEFORE it is captured must produce
    a use-before-capture dataflow warning even though the raw doc contains the
    double-brace form (not the ``${...}`` form that ``find_references`` detects).

    Before the fix, ``dataflow_warnings`` called ``find_references`` on the raw doc
    and ``{{ticket_id}}`` produced zero references, giving a false clean result.
    """
    suite = {
        "schema": 1,
        "suite": "x / y",
        "servers": {"s": {"plugin": "p"}},
        "steps": [
            # step1 uses ticket_id before it is captured (use-before-capture).
            {
                "id": "step1",
                "server": "s",
                "tool": "get_ticket",
                "args": {"id": "{{ticket_id}}"},  # double-brace, not yet normalised
            },
            # step2 captures ticket_id — but it's too late for step1.
            {
                "id": "step2",
                "server": "s",
                "tool": "create_ticket",
                "args": {},
                "capture": {"ticket_id": "$.id"},
            },
        ],
    }
    warnings = suites.dataflow_warnings(suite)
    assert any("ticket_id" in w for w in warnings), (
        f"Expected a use-before-capture warning for ticket_id, got: {warnings}"
    )


def test_dataflow_no_warning_double_brace_after_capture():
    """A suite using ``{{ticket_id}}`` AFTER it is captured must NOT produce a
    dataflow warning — the reference is legitimately available."""
    suite = {
        "schema": 1,
        "suite": "x / y",
        "servers": {"s": {"plugin": "p"}},
        "steps": [
            {
                "id": "step1",
                "server": "s",
                "tool": "create_ticket",
                "args": {},
                "capture": {"ticket_id": "$.id"},
            },
            # step2 uses ticket_id after it is captured — must be clean.
            {
                "id": "step2",
                "server": "s",
                "tool": "get_ticket",
                "args": {"id": "{{ticket_id}}"},  # double-brace form, legitimately bound
            },
        ],
    }
    warnings = [w for w in suites.dataflow_warnings(suite) if "ticket_id" in w]
    assert warnings == [], (
        f"Expected no dataflow warning for ticket_id used after capture, got: {warnings}"
    )


def test_dataflow_no_warning_for_double_brace_run_id():
    """``{{run_id}}`` (double-brace) must NOT produce a dataflow warning — the runner
    always binds both ``run_id`` and ``RUN_ID``.  The normalisation inside
    ``dataflow_warnings`` must not accidentally treat ``run_id`` as unbound."""
    suite = {
        "schema": 1,
        "suite": "x / y",
        "servers": {"s": {"plugin": "p"}},
        "steps": [
            {
                "id": "step1",
                "server": "s",
                "tool": "create_ticket",
                "args": {"title": "wt-{{run_id}}"},  # double-brace run_id
            }
        ],
    }
    warnings = suites.dataflow_warnings(suite)
    assert not any("run_id" in w for w in warnings), (
        f"Expected no warning for {{{{run_id}}}}, got: {warnings}"
    )


# --------------------------------------------------------------------------
# Ticket #18: ExceptionGroup / BaseExceptionGroup from anyio TaskGroup must
# not crash _replay — it must return a structured report instead.
# --------------------------------------------------------------------------

# A minimal suite doc whose server init we will monkeypatch to raise.
_EG_SUITE_DOC: dict[str, Any] = {
    "schema": 1,
    "suite": "test / eg-crash",
    "servers": {"s": {"plugin": "fake"}},
    "steps": [
        {"id": "step1", "server": "s", "tool": "fake_tool"},
    ],
}


@pytest.mark.anyio
async def test_replay_exception_group_returns_structured_report(monkeypatch, tmp_path):
    """Regression (#18): ExceptionGroup raised during server init must be caught
    and returned as a structured report with result='error', init_ok=False,
    and the inner exception's message visible in entry['error'].

    Fails on unfixed code (ExceptionGroup escapes the `except Exception` guard
    and propagates out of _replay). Passes after Fix 1.
    """
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    sdir = tmp_path / "mcp-suites"
    sdir.mkdir(exist_ok=True)
    (sdir / "targets.yaml").write_text("{}", encoding="utf-8")

    import mcp_tester_plugin.runner as _runner
    from mcp_tester_plugin import resolve as _resolve

    # Patch resolve.resolve to return a minimal launch object so the per-server
    # loop reaches stdio_client rather than failing at resolution.
    class _FakeLaunch:
        source = "override"
        server = "fake-server"
        command = "fake-cmd"
        args: list = []

    monkeypatch.setattr(_resolve, "resolve", lambda *a, **kw: _FakeLaunch())

    # Patch stdio_client to raise ExceptionGroup during server init.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def boom_stdio_client(_params):
        raise ExceptionGroup("boom", [RuntimeError("inner-eg")])
        yield  # pragma: no cover

    monkeypatch.setattr(_runner, "stdio_client", boom_stdio_client)

    result = await _runner._replay(_EG_SUITE_DOC, tmp_path, {}, "continue")

    assert isinstance(result, dict), f"expected dict, got {type(result)}: {result!r}"
    assert result.get("run_id") is not None, "run_id must be set"
    assert result.get("result") == "error", f"expected result='error', got: {result.get('result')!r}"
    assert result.get("servers"), "servers list must be non-empty"
    server_entry = result["servers"][0]
    assert server_entry.get("init_ok") is False, f"init_ok must be False: {server_entry}"
    assert "inner-eg" in server_entry.get("error", ""), (
        f"inner exception message must appear in error: {server_entry.get('error')!r}"
    )
    # Steps are still attempted but skipped (server not initialized); the list is non-None.
    assert isinstance(result.get("steps"), list), (
        f"steps must be a list: {result.get('steps')!r}"
    )


@pytest.mark.anyio
async def test_replay_base_exception_group_returns_structured_report(monkeypatch, tmp_path):
    """Regression (#18): BaseExceptionGroup (the base spelling) must also be caught
    and surfaced as a structured report. Ensures both ExceptionGroup and its base
    class are handled by Fix 1.
    """
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    sdir = tmp_path / "mcp-suites"
    sdir.mkdir(exist_ok=True)
    (sdir / "targets.yaml").write_text("{}", encoding="utf-8")

    import mcp_tester_plugin.runner as _runner
    from mcp_tester_plugin import resolve as _resolve

    class _FakeLaunch:
        source = "override"
        server = "fake-server"
        command = "fake-cmd"
        args: list = []

    monkeypatch.setattr(_resolve, "resolve", lambda *a, **kw: _FakeLaunch())

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def boom_stdio_client(_params):
        raise BaseExceptionGroup("boom-base", [ValueError("base-inner")])
        yield  # pragma: no cover

    monkeypatch.setattr(_runner, "stdio_client", boom_stdio_client)

    result = await _runner._replay(_EG_SUITE_DOC, tmp_path, {}, "continue")

    assert result.get("result") == "error"
    server_entry = result["servers"][0]
    assert server_entry.get("init_ok") is False
    assert "base-inner" in server_entry.get("error", ""), (
        f"inner exception must appear in error: {server_entry.get('error')!r}"
    )


@pytest.mark.anyio
async def test_replay_outer_exception_group_returns_structured_report(monkeypatch, tmp_path):
    """Regression (#18): ExceptionGroup escaping the per-server guard (e.g. raised
    during step execution or TaskGroup teardown) must be caught by the outer
    try/except BaseException around AsyncExitStack and returned as a structured
    report with result='error' and a 'phase' key.

    Exercises Fix 2's outer guard by monkeypatching _exec_step to raise.
    """
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)
    (tmp_path / "mcp-suites" / "targets.yaml").write_text("{}", encoding="utf-8")

    import mcp_tester_plugin.runner as _runner

    # Patch _replay's inner dependencies: stub _referenced_servers to return []
    # so the per-server loop is skipped (no real stdio_client needed), then
    # patch _exec_step to raise ExceptionGroup during step execution.
    monkeypatch.setattr(_runner, "_referenced_servers", lambda doc, specs: [])

    async def boom_exec_step(*_args, **_kwargs):
        raise ExceptionGroup("step-boom", [OSError("step-inner")])

    monkeypatch.setattr(_runner, "_exec_step", boom_exec_step)

    result = await _runner._replay(_EG_SUITE_DOC, tmp_path, {}, "continue")

    assert isinstance(result, dict), f"expected dict, got: {result!r}"
    assert result.get("result") == "error", f"expected result='error', got: {result.get('result')!r}"
    assert "phase" in result, f"outer guard must include 'phase' key: {result}"
    assert "step-inner" in result.get("error", ""), (
        f"inner exception must appear in error: {result.get('error')!r}"
    )


@pytest.mark.anyio
async def test_validate_suite_async_exception_group_does_not_reraise(monkeypatch, tmp_path):
    """Regression (#18): if _replay raises ExceptionGroup inside validate_suite_async,
    the exception must not propagate — _replay's outer guard catches it and returns
    a structured report, so validate_suite_async gets a dict back with result='error'
    under the 'verify_replay' key.

    This pins the contract: validate_suite_async must always return a dict, never raise
    BaseExceptionGroup from a verify-replay run.
    """
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    import mcp_tester_plugin.runner as _runner

    # Patch _replay itself to raise ExceptionGroup (simulating the unfixed path).
    async def replay_raises(*_args, **_kwargs):
        raise ExceptionGroup("crash", [RuntimeError("inner-validate")])

    monkeypatch.setattr(_runner, "_replay", replay_raises)

    # Write a valid suite file so the file-path branch is taken.
    _make_suite_file(tmp_path)

    result = await _runner.validate_suite_async("test__minimal", verify_replay=True)

    # Must return a dict, not raise.
    assert isinstance(result, dict), f"expected dict, got: {result!r}"
    # The verify_replay sub-dict must reflect the error.
    assert "verify_replay" in result, f"verify_replay key missing: {result}"
    assert result["verify_replay"].get("result") == "error", (
        f"verify_replay result must be 'error', got: {result['verify_replay'].get('result')!r}"
    )


@pytest.mark.anyio
async def test_server_validate_suite_exception_group_returns_dict(monkeypatch, tmp_path):
    """Regression (#18 / #19): BaseException raised from runner.validate_suite_async
    must be caught by server.validate_suite and returned as a structured dict with
    valid=False and an error key, not propagated as an unhandled crash.

    Exercises Fix 3 in server.py.
    """
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server

    async def boom_validate(*_args, **_kwargs):
        raise BaseException("taskgroup crash")

    monkeypatch.setattr(_runner, "validate_suite_async", boom_validate)

    result = await server.validate_suite("any-suite", verify_replay=True)

    assert isinstance(result, dict), f"expected dict, got: {type(result)}: {result!r}"
    assert result.get("valid") is False, f"valid must be False on crash: {result}"
    assert "error" in result, f"error key must be present: {result}"
    assert "taskgroup crash" in result.get("error", ""), (
        f"error message must include original message: {result.get('error')!r}"
    )


@pytest.mark.anyio
async def test_server_run_suite_exception_group_caught(monkeypatch):
    """Regression (#18): ExceptionGroup raised from runner.run_async must be caught
    by server.run_suite (Fix 4) and returned as a structured dict with result='error'.

    Complements the existing RuntimeError test; specifically exercises the
    BaseExceptionGroup branch of the widened except clause.
    """
    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server

    async def boom(suite, *, policy, **kwargs):
        raise ExceptionGroup("eg", [RuntimeError("eg-inner")])

    monkeypatch.setattr(_runner, "run_async", boom)

    result = await server.run_suite("test__minimal", policy="continue")

    assert isinstance(result, dict), f"expected dict, got: {result!r}"
    assert result.get("result") == "error", f"expected result='error': {result}"
    assert "eg-inner" in result.get("error", ""), (
        f"inner exception message must appear in error: {result.get('error')!r}"
    )


# --------------------------------------------------------------------------
# Ticket #19: ExceptionGroup unwrapping + validate_suite error handling
# --------------------------------------------------------------------------

def test_unwrap_exception_extracts_inner_from_exception_group():
    """_unwrap_exception on an ExceptionGroup must surface the inner exception type
    and message so callers see the actual cause, not the opaque group wrapper."""
    eg = ExceptionGroup("group", [RuntimeError("server spawn failed")])
    result = runner._unwrap_exception(eg)
    assert "RuntimeError" in result, f"Expected 'RuntimeError' in {result!r}"
    assert "server spawn failed" in result, f"Expected 'server spawn failed' in {result!r}"


def test_unwrap_exception_plain_exception():
    """_unwrap_exception on a plain exception returns 'ClassName: message' without
    any unwrapping."""
    result = runner._unwrap_exception(ValueError("bad"))
    assert result == "ValueError: bad", f"Unexpected result: {result!r}"


def test_unwrap_exception_nested_exception_group():
    """_unwrap_exception on a two-level nested ExceptionGroup surfaces only the
    first inner exception (one level of unwrapping)."""
    inner_eg = ExceptionGroup("inner", [TypeError("type problem")])
    outer_eg = ExceptionGroup("outer", [inner_eg])
    result = runner._unwrap_exception(outer_eg)
    # The outer group's first exception is inner_eg, an ExceptionGroup itself.
    # We only unwrap one level — the inner ExceptionGroup type name must appear.
    assert "ExceptionGroup" in result, f"Expected 'ExceptionGroup' in {result!r}"


def test_unwrap_exception_empty_exceptions_list():
    """_unwrap_exception falls back to plain format when .exceptions is an empty list,
    avoiding an IndexError on exc.exceptions[0]."""
    class _FakeGroup(Exception):
        exceptions: list = []

    obj = _FakeGroup("empty group")
    result = runner._unwrap_exception(obj)
    # Falls through to the plain format path.
    assert "_FakeGroup" in result, f"Expected '_FakeGroup' in {result!r}"
    assert "empty group" in result, f"Expected 'empty group' in {result!r}"
    # Must NOT contain the 'caused by' clause.
    assert "caused by" not in result, f"Unexpected 'caused by' in {result!r}"


@pytest.mark.anyio
async def test_run_suite_error_dict_unwraps_exception_group(monkeypatch):
    """run_suite (MCP tool) must unwrap an ExceptionGroup raised by run_async so
    the error string names the inner exception rather than the opaque group."""
    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server

    async def raise_eg(suite, *, policy, **kwargs):
        raise ExceptionGroup("g", [RuntimeError("inner boom")])

    monkeypatch.setattr(_runner, "run_async", raise_eg)

    result = await server.run_suite("test__minimal", policy="continue")

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("result") == "error", f"Expected result='error', got: {result}"
    assert "RuntimeError" in result["error"], (
        f"Expected 'RuntimeError' in error string, got: {result['error']!r}"
    )
    assert "inner boom" in result["error"], (
        f"Expected 'inner boom' in error string, got: {result['error']!r}"
    )


@pytest.mark.anyio
async def test_validate_suite_mcp_tool_returns_error_dict_on_exception(monkeypatch):
    """validate_suite (MCP tool) must catch non-SuiteError exceptions from
    validate_suite_async and return a structured error dict with valid=True.

    A RuntimeError from _replay is a runtime crash that occurs AFTER schema
    validation passed.  Per the documented contract ("valid reflects schema
    validity only") valid must remain True; only a SuiteError signals an
    invalid schema.
    """
    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server

    async def raise_exc(*args, **kwargs):
        raise RuntimeError("replay exploded")

    monkeypatch.setattr(_runner, "validate_suite_async", raise_exc)

    result = await server.validate_suite("test__minimal", verify_replay=True)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("valid") is True, (
        f"Expected valid=True for a non-SuiteError crash (schema was valid), got: {result}"
    )
    assert "error" in result, f"Expected 'error' key, got: {result}"
    assert "RuntimeError" in result["error"], (
        f"Expected 'RuntimeError' in error string, got: {result['error']!r}"
    )


@pytest.mark.anyio
async def test_validate_suite_mcp_tool_unwraps_exception_group(monkeypatch):
    """validate_suite (MCP tool) must unwrap an ExceptionGroup so the inner
    exception type and message are visible in the returned error string.

    An ExceptionGroup from _replay is a runtime crash that occurs AFTER schema
    validation passed.  Per the "valid reflects schema validity only" contract,
    valid must remain True.
    """
    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server

    async def raise_eg(*args, **kwargs):
        raise ExceptionGroup("g", [OSError("pipe broken")])

    monkeypatch.setattr(_runner, "validate_suite_async", raise_eg)

    result = await server.validate_suite("test__minimal", verify_replay=True)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("valid") is True, (
        f"Expected valid=True for ExceptionGroup crash (schema was valid), got: {result}"
    )
    assert "OSError" in result["error"], (
        f"Expected 'OSError' in error string, got: {result['error']!r}"
    )
    assert "pipe broken" in result["error"], (
        f"Expected 'pipe broken' in error string, got: {result['error']!r}"
    )


# --------------------------------------------------------------------------
# Ticket #18 review: CancelledError (and KeyboardInterrupt) must PROPAGATE,
# never be swallowed into a structured-error dict.
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_replay_per_server_init_cancelled_error_propagates(monkeypatch, tmp_path):
    """CancelledError raised by stdio_client during per-server init must propagate
    out of _replay unchanged — NOT be swallowed into a structured error dict.

    Regression guard for the per-server init guard (catch site 1 in _replay).
    """
    import asyncio

    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)
    (tmp_path / "mcp-suites" / "targets.yaml").write_text("{}", encoding="utf-8")

    import mcp_tester_plugin.runner as _runner
    from mcp_tester_plugin import resolve as _resolve

    class _FakeLaunch:
        source = "override"
        server = "fake-server"
        command = "fake-cmd"
        args: list = []

    monkeypatch.setattr(_resolve, "resolve", lambda *a, **kw: _FakeLaunch())

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def cancelled_stdio_client(_params):
        raise asyncio.CancelledError()
        yield  # pragma: no cover

    monkeypatch.setattr(_runner, "stdio_client", cancelled_stdio_client)

    with pytest.raises(asyncio.CancelledError):
        await _runner._replay(_EG_SUITE_DOC, tmp_path, {}, "continue")


@pytest.mark.anyio
async def test_replay_outer_guard_cancelled_error_propagates(monkeypatch, tmp_path):
    """CancelledError raised during step execution must propagate out of _replay
    (through the outer AsyncExitStack guard — catch site 2 in _replay).

    Uses the same monkeypatch idiom as test_replay_outer_exception_group_returns_structured_report
    but asserts the inverse: the fatal signal must escape, not be structured.
    """
    import asyncio

    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)
    (tmp_path / "mcp-suites" / "targets.yaml").write_text("{}", encoding="utf-8")

    import mcp_tester_plugin.runner as _runner

    monkeypatch.setattr(_runner, "_referenced_servers", lambda doc, specs: [])

    async def cancelled_exec_step(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(_runner, "_exec_step", cancelled_exec_step)

    with pytest.raises(asyncio.CancelledError):
        await _runner._replay(_EG_SUITE_DOC, tmp_path, {}, "continue")


@pytest.mark.anyio
async def test_validate_suite_async_cancelled_error_propagates(monkeypatch, tmp_path):
    """CancelledError raised by _replay inside validate_suite_async must propagate
    out (catch site 3 — the belt-and-suspenders guard around await _replay).

    The belt-and-suspenders try/except must NOT swallow cancellation.
    """
    import asyncio

    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    _make_suite_file(tmp_path)

    import mcp_tester_plugin.runner as _runner

    async def replay_cancelled(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(_runner, "_replay", replay_cancelled)

    with pytest.raises(asyncio.CancelledError):
        await _runner.validate_suite_async("test__minimal", verify_replay=True)


@pytest.mark.anyio
async def test_server_run_suite_cancelled_error_propagates(monkeypatch):
    """CancelledError raised by runner.run_async must propagate out of server.run_suite
    (catch site 4 in server.py) — NOT be returned as an error dict.
    """
    import asyncio

    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server

    async def boom(suite, *, policy, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(_runner, "run_async", boom)

    with pytest.raises(asyncio.CancelledError):
        await server.run_suite("test__minimal", policy="continue")


@pytest.mark.anyio
async def test_server_validate_suite_cancelled_error_propagates(monkeypatch, tmp_path):
    """CancelledError raised by runner.validate_suite_async must propagate out of
    server.validate_suite (catch site 5 in server.py) — NOT be returned as
    a structured dict with valid=False.
    """
    import asyncio

    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server

    async def boom(*_args, **_kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(_runner, "validate_suite_async", boom)

    with pytest.raises(asyncio.CancelledError):
        await server.validate_suite("any-suite", verify_replay=True)


@pytest.mark.anyio
async def test_replay_per_server_init_keyboard_interrupt_propagates(monkeypatch, tmp_path):
    """KeyboardInterrupt raised during per-server init must also propagate (not be
    swallowed). Covers the KeyboardInterrupt branch of _reraise_if_fatal.
    """
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)
    (tmp_path / "mcp-suites" / "targets.yaml").write_text("{}", encoding="utf-8")

    import mcp_tester_plugin.runner as _runner
    from mcp_tester_plugin import resolve as _resolve

    class _FakeLaunch:
        source = "override"
        server = "fake-server"
        command = "fake-cmd"
        args: list = []

    monkeypatch.setattr(_resolve, "resolve", lambda *a, **kw: _FakeLaunch())

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def ki_stdio_client(_params):
        raise KeyboardInterrupt()
        yield  # pragma: no cover

    monkeypatch.setattr(_runner, "stdio_client", ki_stdio_client)

    with pytest.raises(KeyboardInterrupt):
        await _runner._replay(_EG_SUITE_DOC, tmp_path, {}, "continue")


# --------------------------------------------------------------------------
# Ticket #18 targeted fix: BaseExceptionGroup wrapping CancelledError must
# propagate — not be swallowed into a structured-error dict.
# anyio delivers cancellation during TaskGroup/AsyncExitStack teardown as
# BaseExceptionGroup("...", [CancelledError()]), whose top-level type is NOT
# CancelledError. The fixed _reraise_if_fatal now recurses into the group.
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_replay_per_server_init_cancelled_group_propagates(monkeypatch, tmp_path):
    """BaseExceptionGroup wrapping CancelledError raised during per-server init
    must propagate out of _replay — NOT be swallowed into a structured error dict.

    Regression guard for the anyio TaskGroup cancellation path: anyio wraps
    CancelledError in a BaseExceptionGroup during teardown. The unfixed
    _reraise_if_fatal only checks isinstance(exc, CancelledError), so the group
    slips past and gets returned as result='error'. The fix recurses into the group.
    """
    import asyncio

    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)
    (tmp_path / "mcp-suites" / "targets.yaml").write_text("{}", encoding="utf-8")

    import mcp_tester_plugin.runner as _runner
    from mcp_tester_plugin import resolve as _resolve

    class _FakeLaunch:
        source = "override"
        server = "fake-server"
        command = "fake-cmd"
        args: list = []

    monkeypatch.setattr(_resolve, "resolve", lambda *a, **kw: _FakeLaunch())

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def cancelled_group_stdio_client(_params):
        raise BaseExceptionGroup("tg", [asyncio.CancelledError()])
        yield  # pragma: no cover

    monkeypatch.setattr(_runner, "stdio_client", cancelled_group_stdio_client)

    # Must propagate the group (or the inner CancelledError), never return a dict.
    with pytest.raises(BaseExceptionGroup):
        await _runner._replay(_EG_SUITE_DOC, tmp_path, {}, "continue")


@pytest.mark.anyio
async def test_replay_outer_guard_cancelled_group_propagates(monkeypatch, tmp_path):
    """BaseExceptionGroup wrapping CancelledError raised during step execution
    must propagate out of _replay's outer AsyncExitStack guard — NOT be
    returned as a structured error dict with result='error'.

    Exercises the outer except BaseException guard (catch site 2 in _replay)
    with the anyio cancellation group shape.
    """
    import asyncio

    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)
    (tmp_path / "mcp-suites" / "targets.yaml").write_text("{}", encoding="utf-8")

    import mcp_tester_plugin.runner as _runner

    monkeypatch.setattr(_runner, "_referenced_servers", lambda doc, specs: [])

    async def cancelled_group_exec_step(*_args, **_kwargs):
        raise BaseExceptionGroup("tg", [asyncio.CancelledError()])

    monkeypatch.setattr(_runner, "_exec_step", cancelled_group_exec_step)

    # Must propagate the group, never return a structured dict.
    with pytest.raises(BaseExceptionGroup):
        await _runner._replay(_EG_SUITE_DOC, tmp_path, {}, "continue")


@pytest.mark.anyio
async def test_replay_nested_cancelled_group_propagates(monkeypatch, tmp_path):
    """Nested BaseExceptionGroup (group-of-groups) containing CancelledError must
    also propagate — _contains_fatal recurses into nested groups.

    Covers the recursive case: BaseExceptionGroup("outer", [
        BaseExceptionGroup("inner", [CancelledError()])
    ]).
    """
    import asyncio

    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    (tmp_path / "mcp-suites").mkdir(exist_ok=True)
    (tmp_path / "mcp-suites" / "targets.yaml").write_text("{}", encoding="utf-8")

    import mcp_tester_plugin.runner as _runner

    monkeypatch.setattr(_runner, "_referenced_servers", lambda doc, specs: [])

    async def nested_cancelled_group_exec_step(*_args, **_kwargs):
        inner = BaseExceptionGroup("inner", [asyncio.CancelledError()])
        raise BaseExceptionGroup("outer", [inner])

    monkeypatch.setattr(_runner, "_exec_step", nested_cancelled_group_exec_step)

    # The nested group must propagate, not be swallowed.
    with pytest.raises(BaseExceptionGroup):
        await _runner._replay(_EG_SUITE_DOC, tmp_path, {}, "continue")


# --------------------------------------------------------------------------
# Ticket #19 reviewer [blocking 1+2]: valid-preservation invariant under raise
# --------------------------------------------------------------------------

def test_sync_validate_suite_valid_preserved_when_anyio_run_raises(monkeypatch, tmp_path):
    """Sync runner.validate_suite must preserve valid=True when anyio.run raises.

    Regression test for the blocking-1 contract violation: the previous
    implementation set valid=False when anyio.run() threw, conflating a runtime
    crash during replay with a schema-invalid suite.  Schema validation passes
    before anyio.run() is called, so out["valid"] is already True at crash time.
    The fix returns {**out, "verify_replay": {...}} without overwriting valid.
    """
    import anyio as _anyio

    def fake_anyio_run(coro_func, *args, **kwargs):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(_anyio, "run", fake_anyio_run)
    _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = runner.validate_suite("test__minimal", verify_replay=True)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("valid") is True, (
        f"valid must be True when replay raises (schema was valid), got: {result}"
    )
    # The error must be surfaced under verify_replay, not as a top-level valid=False.
    assert "verify_replay" in result, (
        f"Expected 'verify_replay' key in result, got: {result}"
    )
    vr = result["verify_replay"]
    assert isinstance(vr, dict), f"verify_replay must be a dict, got: {vr!r}"
    assert "error" in vr, f"Expected 'error' in verify_replay, got: {vr}"
    assert "RuntimeError" in vr["error"], (
        f"Expected 'RuntimeError' in verify_replay.error, got: {vr['error']!r}"
    )
    assert "spawn failed" in vr["error"], (
        f"Expected 'spawn failed' in verify_replay.error, got: {vr['error']!r}"
    )


def test_sync_validate_suite_valid_preserved_on_exception_group(monkeypatch, tmp_path):
    """Sync runner.validate_suite must preserve valid=True when anyio.run raises
    an ExceptionGroup (the common case when anyio wraps child process errors).
    """
    import anyio as _anyio

    def fake_anyio_run(coro_func, *args, **kwargs):
        raise ExceptionGroup("replay", [OSError("child died")])

    monkeypatch.setattr(_anyio, "run", fake_anyio_run)
    _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    result = runner.validate_suite("test__minimal", verify_replay=True)

    assert result.get("valid") is True, (
        f"valid must be True when ExceptionGroup raised (schema was valid), got: {result}"
    )
    vr = result.get("verify_replay", {})
    assert "error" in vr, f"Expected error in verify_replay, got: {result}"
    assert "OSError" in vr["error"], (
        f"Expected 'OSError' in verify_replay.error, got: {vr['error']!r}"
    )
    assert "child died" in vr["error"], (
        f"Expected 'child died' in verify_replay.error, got: {vr['error']!r}"
    )


@pytest.mark.anyio
async def test_server_validate_suite_valid_true_on_exception_group(monkeypatch, tmp_path):
    """server.validate_suite (MCP tool) must return valid=True when _replay raises
    an ExceptionGroup (schema-valid suite, runtime crash during replay).

    This tests the two-exception-type dispatch in server.validate_suite: only a
    SuiteError means the schema was invalid; any other exception is a replay crash
    and must not pollute the valid flag.
    """
    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server

    async def raise_eg(*args, **kwargs):
        raise ExceptionGroup("replay", [ConnectionError("server unreachable")])

    monkeypatch.setattr(_runner, "validate_suite_async", raise_eg)

    result = await server.validate_suite("test__minimal", verify_replay=True)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("valid") is True, (
        f"Expected valid=True for ExceptionGroup (schema was valid), got: {result}"
    )
    assert "error" in result, f"Expected 'error' key in result, got: {result}"
    assert "ConnectionError" in result["error"], (
        f"Expected 'ConnectionError' in error, got: {result['error']!r}"
    )
    assert "server unreachable" in result["error"], (
        f"Expected 'server unreachable' in error, got: {result['error']!r}"
    )


@pytest.mark.anyio
async def test_server_validate_suite_valid_false_on_suite_error(monkeypatch):
    """server.validate_suite (MCP tool) must return valid=False when
    validate_suite_async raises SuiteError (genuine schema-invalid input).

    This pins the correct-side of the two-exception-type dispatch: a SuiteError
    IS a schema problem and must yield valid=False.
    """
    from mcp_tester_plugin import runner as _runner
    from mcp_tester_plugin import server
    from mcp_tester_plugin import suites as _suites

    async def raise_suite_error(*args, **kwargs):
        raise _suites.SuiteError("suite must have a top-level 'schema: 1' field (got None)")

    monkeypatch.setattr(_runner, "validate_suite_async", raise_suite_error)

    result = await server.validate_suite("bad_suite", verify_replay=False)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("valid") is False, (
        f"Expected valid=False for SuiteError (schema invalid), got: {result}"
    )
    assert "error" in result, f"Expected 'error' key in result, got: {result}"
    assert "schema: 1" in result["error"], (
        f"Expected schema error message in error, got: {result['error']!r}"
    )


# --------------------------------------------------------------------------
# Ticket #19 blocking: malformed YAML must return valid=False, not valid=True
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_server_validate_suite_malformed_yaml_returns_valid_false(monkeypatch, tmp_path):
    """Regression: passing syntactically-broken YAML to server.validate_suite must
    return valid=False (not valid=True).

    Before the fix, yaml.YAMLError from yaml.safe_load() was NOT a SuiteError, so
    it fell into the 'except Exception' handler in server.validate_suite and returned
    valid=True — telling the caller a malformed suite was schema-valid.  The fix
    wraps yaml.safe_load() so YAMLError is re-raised as SuiteError, which the
    SuiteError handler catches and returns valid=False.
    """
    from mcp_tester_plugin import server

    # Syntactically broken YAML — unclosed flow mapping triggers ScannerError.
    broken_yaml = "{{ not valid yaml at all"

    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))
    # No mcp-suites/ dir so the input cannot resolve as a file path, forcing
    # the inline-YAML branch where yaml.safe_load is called.

    result = await server.validate_suite(broken_yaml, verify_replay=False)

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("valid") is False, (
        f"Malformed YAML must return valid=False, got: {result}"
    )
    assert "error" in result, f"Expected 'error' key in result, got: {result}"


@pytest.mark.anyio
async def test_validate_suite_async_malformed_yaml_raises_suite_error(monkeypatch, tmp_path):
    """runner.validate_suite_async must raise SuiteError (not yaml.YAMLError) when
    the inline input is syntactically broken YAML.

    This confirms the fix is in the right layer: SuiteError propagates cleanly to
    server.validate_suite's except-SuiteError branch, yielding valid=False.
    """
    import yaml as _yaml

    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    with pytest.raises(suites.SuiteError) as exc_info:
        await runner.validate_suite_async("{{ not valid yaml", verify_replay=False)

    # Must be SuiteError, not raw YAMLError.
    assert not isinstance(exc_info.value, _yaml.YAMLError), (
        "validate_suite_async must wrap YAMLError as SuiteError, not propagate it raw"
    )
    assert "not valid YAML" in str(exc_info.value) or "YAML" in str(exc_info.value), (
        f"Error message should mention YAML, got: {exc_info.value!r}"
    )


def test_suites_load_malformed_yaml_raises_suite_error(tmp_path):
    """suites.load on a file with syntactically broken YAML must raise SuiteError
    (not yaml.YAMLError), so the file-path branch of validate_suite also returns
    valid=False via the SuiteError handler.
    """
    import yaml as _yaml

    sdir = tmp_path / "mcp-suites"
    sdir.mkdir()
    broken_file = sdir / "broken.yaml"
    broken_file.write_text("key: [\nnot closed", encoding="utf-8")

    with pytest.raises(suites.SuiteError) as exc_info:
        suites.load(broken_file)

    assert not isinstance(exc_info.value, _yaml.YAMLError), (
        "suites.load must wrap YAMLError as SuiteError, not propagate it raw"
    )
    assert "invalid YAML" in str(exc_info.value) or "YAML" in str(exc_info.value), (
        f"Error message should mention YAML, got: {exc_info.value!r}"
    )


# --------------------------------------------------------------------------
# Ticket #23: path-resolve failures classified as harness
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_exec_step_expect_path_miss_classified_harness():
    """Test A: expect path that does not resolve → class=harness, routing=tool-surface.

    Records a step with expect: [{path: "$.id", ...}] but the session returns
    {"result": {"id": 42}} — the correct path would be $.result.id. The $.id
    path does not resolve, so the assertion engine sets error="path did not resolve".
    All failed assertions have that error, so the regression must be class=harness
    (suite-authoring defect) not class=behavioural (MCP bug).
    """
    session = _FakeSession(result_text='{"result": {"id": 42}}')
    sessions = {"s": (session, {"create_ticket"}, "fake-mcp")}
    step = {
        "id": "create",
        "server": "s",
        "tool": "create_ticket",
        "args": {},
        "expect": [{"path": "$.id", "op": "equals", "value": 42}],
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "fail", f"expected fail, got: {result}"
    assert len(regressions) == 1, f"expected one regression, got: {regressions}"
    assert regressions[0]["class"] == "harness", (
        f"expected class=harness for path-miss, got: {regressions[0]['class']!r}"
    )
    assert regressions[0]["routing"] == "tool-surface", (
        f"expected routing=tool-surface, got: {regressions[0]['routing']!r}"
    )


@pytest.mark.anyio
async def test_exec_step_capture_path_miss_classified_harness():
    """Test B: capture path that does not resolve → class=harness, routing=tool-surface.

    Records a step with capture: {wt_id: "$.id"} but the session returns
    {"result": {"id": 99}} — the correct capture path would be $.result.id.
    A capture path that does not resolve is always a suite-authoring defect.
    """
    session = _FakeSession(result_text='{"result": {"id": 99}}')
    sessions = {"s": (session, {"create_worktree"}, "fake-mcp")}
    step = {
        "id": "create",
        "server": "s",
        "tool": "create_worktree",
        "args": {},
        "capture": {"wt_id": "$.id"},
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "fail", f"expected fail, got: {result}"
    assert len(regressions) == 1, f"expected one regression, got: {regressions}"
    assert regressions[0]["class"] == "harness", (
        f"expected class=harness for capture path-miss, got: {regressions[0]['class']!r}"
    )
    assert regressions[0]["routing"] == "tool-surface", (
        f"expected routing=tool-surface, got: {regressions[0]['routing']!r}"
    )


@pytest.mark.anyio
async def test_exec_step_mixed_expect_failures_classified_behavioural():
    """Test C: mixed failures — one path-miss AND one value-wrong → class=behavioural.

    When not ALL failed assertions are path-did-not-resolve, the failure is not
    purely a suite-authoring defect; at least one assertion resolved correctly but
    returned the wrong value, so the MCP's behaviour is the likely cause.

    Uses op="equals" on the non-resolving path (not op="exists") because
    assertions.evaluate for op="exists" returns early WITHOUT setting
    error="path did not resolve" — so a genuine mixed-failure boundary requires
    an op that needs the path to resolve (equals, matches, etc.).
    """
    # $.result.id resolves to 42; $.missing does not resolve.
    session = _FakeSession(result_text='{"result": {"id": 42}}')
    sessions = {"s": (session, {"get_thing"}, "fake-mcp")}
    step = {
        "id": "check",
        "server": "s",
        "tool": "get_thing",
        "args": {},
        "expect": [
            # This resolves (value is 42) but we assert 999 → value-wrong, no error key.
            {"path": "$.result.id", "op": "equals", "value": 999},
            # This does not resolve with op=equals → error="path did not resolve".
            {"path": "$.missing", "op": "equals", "value": "x"},
        ],
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "fail", f"expected fail, got: {result}"
    assert len(regressions) == 1, f"expected one regression, got: {regressions}"
    assert regressions[0]["class"] == "behavioural", (
        f"expected class=behavioural for mixed failures, got: {regressions[0]['class']!r}"
    )
    # Behavioural routing must be distinct from the harness/tool-surface case.
    assert regressions[0]["routing"] == "behaviour", (
        f"expected routing=behaviour for behavioural class, got: {regressions[0]['routing']!r}"
    )


@pytest.mark.anyio
async def test_exec_step_value_wrong_classified_behavioural():
    """Test D: path resolves but value is wrong → class=behavioural (pure MCP defect).

    $.result.id resolves (value=1), but the suite asserts value=999.
    No path-miss involved — this is a genuine behavioural regression in the MCP.
    """
    session = _FakeSession(result_text='{"result": {"id": 1}}')
    sessions = {"s": (session, {"get_thing"}, "fake-mcp")}
    step = {
        "id": "check",
        "server": "s",
        "tool": "get_thing",
        "args": {},
        "expect": [{"path": "$.result.id", "op": "equals", "value": 999}],
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "fail", f"expected fail, got: {result}"
    assert len(regressions) == 1, f"expected one regression, got: {regressions}"
    assert regressions[0]["class"] == "behavioural", (
        f"expected class=behavioural for value-wrong, got: {regressions[0]['class']!r}"
    )


@pytest.mark.anyio
async def test_exec_step_multiple_value_wrong_classified_behavioural():
    """Test E: multiple assertions all resolve but values are wrong → class=behavioural.

    Sanity check that the all-path-miss guard does not fire when none of the
    assertions have error="path did not resolve" (they all resolved, just wrong values).
    """
    session = _FakeSession(result_text='{"result": {"id": 1, "status": "open"}}')
    sessions = {"s": (session, {"get_thing"}, "fake-mcp")}
    step = {
        "id": "check",
        "server": "s",
        "tool": "get_thing",
        "args": {},
        "expect": [
            {"path": "$.result.id", "op": "equals", "value": 999},
            {"path": "$.result.status", "op": "equals", "value": "closed"},
        ],
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "fail", f"expected fail, got: {result}"
    assert len(regressions) == 1, f"expected one regression, got: {regressions}"
    assert regressions[0]["class"] == "behavioural", (
        f"expected class=behavioural for all-value-wrong, got: {regressions[0]['class']!r}"
    )


class _FakeErrorSession:
    """Duck-typed ClientSession that returns an isError=True result."""
    def __init__(self, text: str = "tool error occurred"):
        self._text = text
        self.called_with: list[tuple[str, dict]] = []

    async def call_tool(self, tool: str, arguments: dict) -> _FakeCallResult:
        self.called_with.append((tool, arguments))
        return _FakeCallResult(text=self._text, is_error=True)


@pytest.mark.anyio
async def test_exec_step_is_error_no_expect_classified_behavioural():
    """Regression (blocking 1): a tool that returns isError=True with NO expect
    entries must classify as class='behavioural', NOT class='harness'.

    Before the fix, ``all_path_miss = all(... for a in [])`` vacuously returned
    True when ``failed`` was empty (is_error/no-expect branch sets all_ok=False
    but never populates assertion_results), so the regression was mislabelled
    as class='harness' (suite-authoring defect) instead of class='behavioural'
    (MCP returned an error).
    """
    session = _FakeErrorSession(text="internal server error")
    sessions = {"s": (session, {"do_thing"}, "fake-mcp")}
    step = {
        "id": "errstep",
        "server": "s",
        "tool": "do_thing",
        "args": {},
        # Deliberately NO "expect" entries — the is_error/no-expect branch.
    }
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "fail", f"expected fail, got: {result}"
    assert result.get("is_error") is True, f"expected is_error=True, got: {result}"
    assert len(regressions) == 1, f"expected one regression, got: {regressions}"
    assert regressions[0]["class"] == "behavioural", (
        f"expected class=behavioural for is_error/no-expect, got: {regressions[0]['class']!r}"
    )
    assert regressions[0]["routing"] == "behaviour", (
        f"expected routing=behaviour for behavioural class, got: {regressions[0]['routing']!r}"
    )


# --------------------------------------------------------------------------
# Ticket #22: BaseExceptionGroup from call_tool must be caught per-step,
# not escape to the outer _replay handler.
# --------------------------------------------------------------------------

class _RaisingSession:
    """Duck-typed session whose call_tool raises the configured exception."""
    def __init__(self, exc: BaseException):
        self._exc = exc

    async def call_tool(self, tool: str, arguments: dict) -> None:
        raise self._exc


@pytest.mark.anyio
async def test_exec_step_base_exception_group_recorded_as_fail():
    """Regression (#22): BaseExceptionGroup from call_tool must be caught per-step
    and recorded as status='fail' with the inner message in 'error', and exactly
    one harness regression appended.  Must not re-raise.

    Before the fix, `except Exception` in _exec_step let BaseExceptionGroup
    escape to the outer _replay handler, discarding remaining steps and producing
    an opaque result='error' report.
    """
    exc = BaseExceptionGroup("transport error", [RuntimeError("connection reset")])
    session = _RaisingSession(exc)
    sessions = {"s": (session, {"fake_tool"}, "fake-mcp")}
    step = {"id": "step1", "server": "s", "tool": "fake_tool"}
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=False)

    assert result["status"] == "fail", f"expected fail, got: {result}"
    assert "connection reset" in result.get("error", ""), (
        f"inner exception message must appear in error: {result.get('error')!r}"
    )
    assert len(regressions) == 1, f"expected one regression, got: {regressions}"
    assert regressions[0]["class"] == "harness", (
        f"expected class=harness for call_tool raise, got: {regressions[0]['class']!r}"
    )


@pytest.mark.anyio
async def test_exec_step_base_exception_group_teardown_recorded_as_fail():
    """Regression (#22): BaseExceptionGroup from call_tool during teardown must be
    caught per-step, set status='fail', populate teardown_note with the step id,
    and NOT append a regression (teardown failures are notes, not regressions).
    """
    exc = BaseExceptionGroup("transport error", [RuntimeError("connection reset")])
    session = _RaisingSession(exc)
    sessions = {"s": (session, {"fake_tool"}, "fake-mcp")}
    step = {"id": "teardown1", "server": "s", "tool": "fake_tool"}
    regressions: list[dict[str, Any]] = []

    result = await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                     is_teardown=True)

    assert result["status"] == "fail", f"expected fail, got: {result}"
    note = result.get("teardown_note", "")
    assert "teardown1" in note, (
        f"teardown_note must contain the step id 'teardown1': {note!r}"
    )
    assert regressions == [], (
        f"teardown failures must not append regressions, got: {regressions}"
    )


@pytest.mark.anyio
async def test_exec_step_cancelled_error_group_reraises():
    """Regression (#22): BaseExceptionGroup wrapping CancelledError raised by
    call_tool must be re-raised by _exec_step (via _reraise_if_fatal), not
    swallowed into a structured-error dict.

    This confirms the _reraise_if_fatal call is made before the error-dict path.
    """
    import asyncio

    exc = BaseExceptionGroup("cancel", [asyncio.CancelledError()])
    session = _RaisingSession(exc)
    sessions = {"s": (session, {"fake_tool"}, "fake-mcp")}
    step = {"id": "step1", "server": "s", "tool": "fake_tool"}
    regressions: list[dict[str, Any]] = []

    with pytest.raises(BaseExceptionGroup):
        await runner._exec_step(step, sessions, {"RUN_ID": "r1"}, regressions,
                                is_teardown=False)
