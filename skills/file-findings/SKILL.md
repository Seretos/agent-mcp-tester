---
name: file-findings
description: File a set of E2E test findings as issue-tracker tickets, autonomously. Use after a black-box MCP test run, when you have findings to record. Routes each finding to the right repo by the wrapper/lib split (behaviour → lib repo, tool surface → wrapper repo, ambiguous → wrapper), groups findings thematically by root cause, labels them (bug / documentation / enhancement + severity + e2e-test), and creates the tickets via whatever issue-tracker MCP is available. If no tracker is reachable or a finding's target project can't be resolved, it falls back to printing the findings directly instead of failing. Does NOT decide what to test — it only files findings handed to it.
---

# file-findings

You take a set of test findings and record them — as issue-tracker tickets when you can, or
printed directly when you can't. You do **not** decide what gets tested or re-test anything; you
work the findings you were given.

The findings may come from an LLM sweep (`sweep-mcp`) or from a deterministic replay
(`replay-suite`): both hand you the same finding shape — a tool, observed-vs-expected, a repro
call, a severity, and a `behaviour`/`tool-surface` routing hint. Treat them identically.

## 0. The ticket connection is soft — decide whether you can file at all

Filing is best-effort, not a hard dependency. Before routing anything, establish whether you can
actually create tickets:

- **No issue-tracker MCP reachable** — there are no project/ticket tools, or they won't load even
  via `ToolSearch`. Then **do not fail**: skip to §5 and print the findings as a clean report so the
  caller still gets them.
- **A tracker is reachable.** Enumerate the projects it exposes (e.g. `list_projects` /
  `find_projects`). That live list — plus any wrapper↔lib mapping in the **host repo's testing
  notes** — is your routing ground truth. This plugin ships no hardcoded repo list on purpose.

Any individual finding whose target you can't resolve (no matching registered project, or the
project lacks create-issue permission) is **not** an error either: keep it in the report (§5) and
say why it wasn't filed. File what you can; print what you can't.

## 1. Route each finding to the right repo

Plugins under test are typically thin **MCP wrappers** over a **lib engine**, or **self-contained**.
Route by where the cause lives:

- **Wrapper over a lib** → behaviour / logic / API semantics / return shapes / error contract /
  pagination / atomicity / provider quirks / data model go to the **lib repo**. Tool surface — tool
  schema, param definitions, required/optional flags, enum docs, docstring-vs-behaviour, naming,
  discoverability — goes to the **wrapper repo**.
- **Self-contained plugin** (no lib) → everything goes to the plugin's own repo.
- **Ambiguous or mixed** → the **wrapper repo wins**.

Which concrete repo is the wrapper and which is the lib for a given MCP is **not** baked into this
skill — read it from the host repo's testing notes / the registered project list (§0). If you have
no mapping for a finding's MCP, route it to the project that matches the MCP's own name if one is
registered, else keep it for direct output.

## 2. Group findings thematically by root cause

Not one ticket per bug, and not everything in one mega-ticket. Group by theme / shared root cause,
e.g. "critical functional defects", "cross-provider error contract", "return-shape inconsistency",
"pagination/limits", "schema/docstring vs behaviour". One ticket per **(theme × target repo)** — a
theme that spans two repos becomes two tickets, each scoped to that repo's findings.

Each finding inside a ticket body carries a **concrete repro call** (the exact tool + args) and
**observed vs. expected**.

## 3. Label each ticket

- **Type** — functional defect → `bug`; agent-intuitiveness / UX / docstring / discoverability finding →
  `documentation` (clarity/description issues) or `enhancement` (missing capability / ergonomic API gap).
- **Severity** — exactly one of `severity:high`, `severity:med`, `severity:low`:
  - `severity:high` — crash, data loss, silent wrong result, or the claimed behaviour outright fails.
  - `severity:med` — wrong behaviour with a workaround, a notable inconsistency, or UX friction that would
    mislead a real agent.
  - `severity:low` — cosmetic, docstring nit, minor polish.
- **Origin** — always `e2e-test`.

If the tracker stamps an AI-attribution marker (e.g. `ai-generated`) automatically, don't add it
yourself.

## 4. Create the tickets

Use the issue-tracker MCP's create-ticket tool, **one at a time, sequentially** — parallel create
bursts can fail silently. For each: the resolved target project, a clear title, the grouped body,
and the labels above.

## 5. Report

When done, list every ticket you created: target repo, title, label set, and URL. Then list every
finding you could **not** file — no reachable tracker, or no resolvable target — with the finding's
repro, observed-vs-expected, severity, and routing hint inline, so nothing is lost.
