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
# Regression: sync run() must NOT be called from within a running event loop
# --------------------------------------------------------------------------
@pytest.mark.anyio
async def test_run_from_running_loop_crashes(monkeypatch, tmp_path):
    """The SYNC runner.run() raises RuntimeError when called from a running loop.

    This documents the original crash (asyncio.run / anyio.run inside an
    already-running loop). The async entry points introduced to fix the bug
    avoid this path entirely.
    """
    async def stub_replay(*_args, **_kwargs) -> dict[str, Any]:
        return STUB_PASS_REPORT

    monkeypatch.setattr(runner, "_replay", stub_replay)
    suite_path = _make_suite_file(tmp_path)
    monkeypatch.setenv("MCP_TESTER_ROOT", str(tmp_path))

    with pytest.raises(RuntimeError):
        runner.run("test__minimal")


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
