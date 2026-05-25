---
name: file-findings
description: File a set of E2E test findings as tickets in the correct Seretos repos. Use after a black-box MCP test run, when you have findings to record. Routes each finding to the right repo by the wrapper/lib split (behaviour → lib repo, tool surface → wrapper repo, ambiguous → wrapper), groups findings thematically by root cause, labels them (bug / documentation / enhancement + severity + e2e-test), and creates the tickets autonomously via the project-issues MCP. Does NOT decide what to test — it only files findings handed to it.
---

# file-findings

You take a set of test findings and file them as tickets in the correct repos, autonomously.
You do **not** decide what gets tested or re-test anything — you work the findings you were given.

The findings may come from an LLM sweep (`sweep-mcp`) or from a deterministic replay (`replay-suite`):
both hand you the same finding shape — a tool, observed-vs-expected, a repro call, a severity, and a
`behaviour`/`tool-surface` routing hint. Treat them identically.

## 1. Route each finding to the right repo

The plugins under test are thin **MCP wrappers** over a **lib engine**, or **self-contained**.
Route by where the cause lives:

- **Wrapper over a lib** → behaviour / logic / API talk / return shapes / error contract / pagination /
  atomicity / provider quirks / data model goes to the **lib repo**. Tool surface — tool schema, param
  definitions, required/optional flags, enum docs, docstring-vs-behaviour, naming, discoverability —
  goes to the **wrapper repo**.
- **Self-contained plugin** (no lib) → everything goes to the plugin's own repo.
- **Ambiguous or mixed** → the **wrapper repo wins**.

Known pairs (extend as the ecosystem grows):

| Wrapper MCP | Lib engine | Behaviour/logic → | Tool surface/UX → |
|---|---|---|---|
| `agent-project-issues` | `lib-python-projects` | `lib-python-projects` | `agent-project-issues` |
| `agent-vdesktop` | `lib-python-vdesktop` | `lib-python-vdesktop` | `agent-vdesktop` |

`lib-python-config` is not a separate target — config findings route via the `lib-python-projects` rule.

If a finding's target repo isn't registered (`list_projects` / `find_projects` doesn't show it, or it
lacks `issues.create`), do not force it — keep that finding in your report and say so.

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
  - `severity:high` — crash, data loss, silent wrong result, or the ticket's claimed behaviour outright fails.
  - `severity:med` — wrong behaviour with a workaround, a notable inconsistency, or UX friction that would
    mislead a real agent.
  - `severity:low` — cosmetic, docstring nit, minor polish.
- **Origin** — always `e2e-test`.

Do **not** add `ai-generated` yourself — the MCP server adds it automatically.

## 4. Create the tickets

Use `create_ticket` on the project-issues MCP, **one at a time, sequentially** — parallel create bursts
fail silently. For each: the resolved `project_id`, a clear title, the grouped body, and the labels above.

## 5. Report

When done, list every ticket you created: target repo, title, label set, and URL. For any finding you
could **not** file (no registered target), say which and why.
