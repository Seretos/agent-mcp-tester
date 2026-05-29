---
name: replay-suite
description: Deterministically replay previously-recorded MCP test suites with ZERO LLM tokens, via the mcp-tester runner. Use to re-check an MCP's behaviour after a change without re-running a full LLM sweep — the runner spawns the target server(s) over stdio JSON-RPC, replays the recorded calls, evaluates assertions, and reports regressions. Routes any regressions into the file-findings skill. Use when the user wants to "replay the suites", "re-run the recorded tests", "regression-check <mcp>", or gate a change. Does NOT author tests — sweep-mcp (record mode) does that.
---

# replay-suite

You re-run already-recorded suites **deterministically**. The expensive reasoning was done once, when
`sweep-mcp` recorded the suite; your job here is cheap orchestration: tell the `mcp-tester` runner which
suites to replay, collect its structured reports, and route any regressions to `file-findings`. The runner
does the actual tool calls — **you do not call the MCP-under-test yourself**, so this spends ~no tokens on
the stable surface. That is the entire point of recording.

The runner talks raw MCP stdio JSON-RPC and resolves each server source-free (it reads only public install
metadata / the published plugin manifest, never MCP source). You never pass it a launch command.

## 1. Resolve which suites to run

From what the user said: a specific suite, all suites for one MCP, or everything. Enumerate the committed
suites with the runner's **`list_suites`** tool
(`mcp__plugin_agent-mcp-tester_mcp-tester__list_suites`; load via `ToolSearch(query="select:...")` if it's
deferred). Suites live in `mcp-suites/` at the root of the repo where you invoke the runner. If there are
none, say so — recording (via `sweep-mcp` record mode) has to happen first.

## 2. Replay each suite

For each in-scope suite, call **`run_suite`**
(`mcp__plugin_agent-mcp-tester_mcp-tester__run_suite`) with the suite name and `policy: "continue"`
(replay every step and surface all regressions in one pass; use `"abort"` only if a destructive early
failure makes later steps meaningless).

You may run independent suites' `run_suite` calls in parallel. The runner generates a fresh `${RUN_ID}` per
run, so re-runs don't collide, and always runs each suite's `teardown`.

Each call returns a structured report:

- `result: "pass"` — every assertion held; nothing to file.
- `result: "regression"` — one or more assertions failed; see `regressions[]`.
- `result: "error"` — a server didn't resolve or didn't initialize (see `servers[].error`). This is a
  **setup/config** problem, not necessarily an MCP bug: the target plugin may not be built/installed, or a
  token (e.g. `GITHUB_TOKEN`) may be missing from the environment. Surface it as such — do not file it as a
  behavioural bug, and do not try to bypass the runner.

## 3. File regressions

Collect the `regressions[]` arrays from every report. Each regression already carries the finding shape
`file-findings` expects — tool, observed-vs-expected, repro, `severity`, and a `behaviour`/`tool-surface`
routing hint (the runner maps its `class` to that hint for you). Hand the combined list to the
**`file-findings`** skill; it does the repo routing, thematic grouping, labeling, and sequential ticket
creation. If every suite passed, there is nothing to file — say so.

## 4. Report

Summarize per suite: `pass` / `regression` / `error`, step counts, and — for regressions filed — the
tickets `file-findings` created (repo, title, URL). Name any suite that errored and why (unresolved server,
missing token), so the user can fix the environment and re-run.

## Notes

- **CI / no-Claude lane.** The same replay runs headless with the runner's CLI:
  `mcp-tester run <suite> --report out.json` (exit 0 on pass, 1 on regression). Mention this when the user
  wants a release gate rather than an interactive check.
- **Live real-system MCPs are not replayed deterministically in v1** — they have no recorded suites by
  design (a headless replay would drive a shared real resource unguarded). Test those via `sweep-mcp`'s
  normal serial path instead.
- You do **not** decide what to test or edit any suite — you replay what's committed and file what breaks.
