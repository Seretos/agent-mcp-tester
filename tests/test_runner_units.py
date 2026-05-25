"""Unit tests for the deterministic runner's pure logic (no MCP, no network)."""

import os

import pytest

from mcp_tester_plugin import assertions, suites


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
