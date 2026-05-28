---
name: sweep-mcp
description: Exhaustively E2E-test a whole MCP plugin's tool surface ("auf Herz und Nieren"). Use when the user wants a full sweep of an MCP, not a single-ticket check. Inspects the tool surface, decomposes it into stateful capability clusters, spawns one black-box cluster-tester subagent per cluster (parallel where independent, serial for live real-system MCPs, fixtures where coupled), collects compact findings digests, and hands them to the file-findings skill. Optionally records repeatable test suites (record mode) for later zero-LLM replay via the deterministic runner. Does NOT decide which MCP is in scope — the user states that at invoke time.
---

# sweep-mcp

You orchestrate a full E2E sweep of one MCP. You inspect its tool surface, split it into capability
clusters, spawn a black-box **`cluster-tester`** worker per cluster, collect their digests, and route the
aggregate findings into the **`file-findings`** skill. You are the orchestrator; the verbose tool-call work
happens inside the workers, not here — that isolation is the whole point.

This isolation is also your main **token lever**: the orchestrator must **never** run the MCP-under-test's
probing tools itself — not even for an MCP you are forced to test serially. "Serial" means *one worker at a
time*, not *you driving the tools inline*. Inline probing dumps every call and response into the
orchestrator context that then still has to do steps 5–6 — the most expensive way to run a sweep. The few
read calls you legitimately make here (reading the tool surface, seeding a fixture, taking a live-system
baseline per §4b) are the exception, not a licence to probe inline.

You do **not** decide *what* gets tested or which MCP is in scope. The user tells you that when they invoke
you. You decide *how* to decompose, spawn, and aggregate.

## 1. Resolve scope

Take the MCP (and any focus, e.g. "only the PR and review clusters") from what the user said at invoke time.
If they named a focus, sweep that subset; otherwise sweep the whole surface. Never substitute your own
target selection.

Also note whether the user asked you to **record** (e.g. "sweep and record suites", "build replayable
tests"). If so, run record mode (§7) alongside the normal sweep. If they didn't ask, just sweep.

## 2. Derive capability clusters from the tool surface

Look at the MCP's available tools and group them into **stateful capability clusters** — coherent
lifecycle domains, not raw alphabetical buckets. (The tools may be deferred here too; your main session has
`ToolSearch`, so load any you need — to read the surface or to seed fixtures in step 3 — before calling.)

- A cluster is a set of tools that share state and are tested as a sequence — typically a
  **create -> read -> update -> delete/merge/remove** lifecycle over one kind of entity.
- Independent, read-only tools (discovery, search, monitor/info, status reads) each form their own light
  cluster — cheap, parallel, no setup.
- **Scale the decomposition to the surface size.** A large MCP -> one worker per cluster. A tiny MCP
  (e.g. a 3-tool lifecycle) -> a single worker, or test it inline. **Do not go finer than a cluster:** a
  per-single-call worker has to rebuild the cluster's state from scratch, which costs *more* tokens, not
  fewer.

Derive the clusters from the *actual* surface in front of you. A typical issue-tracker MCP, for instance,
falls out as ticket-lifecycle / pr-lifecycle / pr-review / comments / relations + a couple of read-only
discovery clusters; a single-entity lifecycle MCP is just one cluster. If the host repo's testing notes
list reference clusters or known hazards for this specific MCP, use them — this skill carries no per-MCP
table of its own.

If the MCP drives a **shared real system** (see §4b), sweep it under the extra rules there, not the parallel
default.

## 3. Handle coupling between clusters

Some clusters depend on another entity existing (comments and relations need a parent entity; a review
needs something to review). Two ways to handle it:

- **Fold** the dependency into one worker (e.g. let the parent-entity worker also touch its comments), **or**
- **Seed a fixture**: create the prerequisite yourself (one create call) and pass its id into the dependent
  worker's `probe_focus`. Prefer this when you want the clusters tested independently and in parallel.

## 4. Spawn the cluster-tester workers

For each cluster, spawn `Agent(subagent_type="cluster-tester", ...)`. Spawn **independent clusters in
parallel** — one message, multiple `Agent` calls. Spawn a coupled worker after its fixture exists.
**Exception:** for a live real-system MCP, spawn serially — never a parallel batch (see §4b).

Each worker prompt must carry: `mcp_name`, `cluster_name`, the exact `tool_list` for that cluster,
`probe_focus` (what to exercise + any seeded fixture ids), the `sandbox_project(s)` it may write to, the
black-box constraints, and the digest format you expect back. A cross-cluster integration journey
(e.g. create entity -> derive child -> comment) is just a worker with a broader `tool_list` and a scripted
`probe_focus` — not a different kind of agent. **In record mode**, also pass `record: true` and a
`server_ref` (the logical handle you'll use for this MCP in the suite, e.g. `t`).

Give `tool_list` as **fully-qualified tool names** (`mcp__plugin_<plugin>_<server>__<tool>`), not friendly
shorthand — the worker feeds them verbatim into `ToolSearch(query="select:...")`. Each worker starts in a
fresh context where these tools are likely **deferred** (callable only after their schemas are loaded); the
`cluster-tester` agent loads its `tool_list` via `ToolSearch` before testing, so the names must be exact.
Schemas you loaded in *this* orchestrator session do **not** carry over to the workers — every worker
bootstraps its own.

Workers are leaf nodes: they don't spawn anything and they don't create tickets.

## 4b. Live / stateful real-system MCPs

Some MCPs act on a **shared real resource the user is actively using** — for example an MCP that drives the
live desktop, real windows, or the terminal hosting this very session. For these, two things change;
everything else in §4 still holds:

- **Serialize every cluster.** Spawn one worker, await its digest, then spawn the next — never a parallel
  batch, not even for the read-only clusters. Multiple agents racing one shared real resource corrupt each
  other's state. Time is not the constraint here; safety is, and serial is fine.
- **Still delegate — do not drive the probing inline.** The shared resource is shared whether a worker
  drives it or you do, so moving the probing into the orchestrator buys *no* safety — it only costs tokens.
  Serial means one worker at a time, not you calling the tools yourself.

The orchestrator owns the **safety baseline** so a worker that dies mid-mutation can't leave the system
broken:

- **Take ONE baseline snapshot up front** (the MCP's read/query tools for current state) and keep it. Pass
  it into each worker's `probe_focus` as the ground-truth state to restore to. Reuse that same snapshot for
  every worker — do **not** re-list state between workers on the happy path (that reintroduces the bloat you
  were avoiding). Re-snapshot and remediate only if a worker reports it could not restore, or its run ended
  abnormally.
- In each worker's `probe_focus`, spell out the live-system rules (the `cluster-tester` agent also enforces
  them): only mutate/clean up what it created; **never destroy a resource it did not itself create**
  (untrack instead of deleting where the MCP allows); restore to the baseline and verify before returning.

**Pass the MCP's known hazards into every worker explicitly** — a worker has no memory of past runs and
cannot see your notes. Read those hazards from the **host repo's testing notes** for this specific MCP
(e.g. tools that act globally rather than on a single handle, or that silently adopt a pre-existing resource
instead of creating a fresh one). This skill stays MCP-agnostic; the concrete hazard list lives with the
host repo, not here.

## 5. Collect digests

Gather each worker's compact digest. Keep them compact — do not ask workers for, or re-expand, raw
tool-call transcripts. If a worker reports a setup/auth failure, note that cluster as blocked and keep going.

## 6. File the findings

Hand the aggregated findings to the **`file-findings`** skill. Routing (wrapper vs. lib engine), labeling
(`bug` / `documentation` / `enhancement` + `severity:*` + `e2e-test`), thematic grouping, **sequential**
ticket creation, and the soft fallback (print directly when no tracker is reachable) all live there — do not
duplicate those rules here. Pass each finding with its repro call, observed vs. expected, the worker's
severity and routing hint, and which MCP/cluster it came from.

## 7. Record mode — persist repeatable suites

Run this only when the user asked you to record. The aim: capture the deterministic core of each cluster
once, so future runs replay it with **zero LLM** via the `replay-suite` skill / the `mcp-tester` runner.

1. **Collect fragments.** Each `cluster-tester` you spawned with `record: true` returns a fenced
   **"Recorded suite fragment"** block (steps + assertions + teardown, referencing only its `server_ref`)
   alongside its digest.
2. **Assemble a full suite per cluster.** You — not the worker — own the parts that touch how the server is
   reached. Wrap each fragment into a complete suite document:

   ```yaml
   schema: 1
   suite: <mcp_name> / <cluster_name>
   targets: [<mcp_name>]
   recorded_at: "<UTC ISO timestamp>"
   recorded_by: cluster-tester
   sandbox: { project: <sandbox_project> }
   run_id: { strategy: token, template: "e2e-{{ts}}-{{rand6}}" }
   servers:
     <server_ref>:                 # the same handle you gave the worker
       plugin: <mcp_name>          # the marketplace plugin name
       server: <server-key>        # the key in that plugin's mcpServers block (omit if it has only one)
       env_passthrough: [<TOKENS>] # names of env vars the target needs; [] if none
   steps:        <from the fragment>
   teardown:     <from the fragment>
   ```

   You add `servers:`, `targets`, `recorded_at`, `sandbox`, `run_id`. The worker supplied `steps` and
   `teardown`. Set each step's `server:` to the fragment's `server_ref` (or rely on the single-server
   default).
3. **Persist with verify-replay.** Hand the assembled YAML to the runner's **`save_suite`** tool
   (`mcp__plugin_agent-mcp-tester_mcp-tester__save_suite`; load via `ToolSearch(query="select:...")` if
   deferred) with `verify_replay: true`. It replays the suite once against the live MCP before writing it
   to `mcp-suites/`. If it returns `saved: false`, the verify-replay caught a bad assertion (almost always a
   volatile field marked `equals` instead of `exists`); read the failing assertions in its report, fix that
   step in the YAML, and re-submit. Do not hand-write suites to disk — always go through `save_suite` so
   every committed suite is known to pass at least once.
4. **Report what you recorded.** List each suite file written under `mcp-suites/`, and call out any cluster
   you could **not** record (e.g. live real-system clusters — see below).

**Do not record live real-system MCPs in v1.** A deterministic replay would drive the shared real resource
with no LLM judgment guarding it. Sweep those the normal way (§4b) and file findings, but skip the
fragment/suite step for those clusters.

## 8. Report

Summarize: which MCP was swept, which clusters were covered (and any blocked), total findings by severity,
and — from `file-findings` — which tickets were created in which repos (with URLs) or which findings were
printed directly. In record mode, also list the suites persisted to `mcp-suites/`.
