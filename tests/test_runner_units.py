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

    with pytest.raises(suites.SuiteError):
        await runner.validate_suite_async(bad_yaml, verify_replay=False)


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
