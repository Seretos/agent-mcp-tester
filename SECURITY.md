# Security Policy

## Threat model

`mcp_tester_plugin` is a **local** MCP server. It runs as a process launched
by an MCP client (typically Claude Code) on the same machine as the user,
with the user's own privileges. It does not listen on a network socket and
is not designed to be exposed beyond the host.

The trust boundary is the MCP client: anything that can reach the server's
stdio already runs as the user. The tools exposed here are accordingly
authority-equivalent to "the user runs commands themselves" — within the
scope of whatever credentials or filesystem permissions the user has.

## Out of scope

- Compromise of the host machine where the plugin runs (the user already
  owns it).
- Misuse of the plugin's tools by a malicious local MCP client — that client
  already runs as the user.

## Reporting a vulnerability

For unexpected authority escalation, input validation gaps that escape the
documented contract of a tool, or any other security concern, open a GitHub
issue with the label `security` (or a private security advisory if the
repository supports them).

---

<!--
EXTEND THIS FILE with plugin-specific sections as the surface area grows.
This plugin spawns OTHER MCP server binaries as subprocesses (the deterministic
runner) and bridges named environment variables (e.g. GITHUB_TOKEN) into those
children. Document below, once the runner lands:

  ## Intentional subprocess execution
  The runner resolves a target MCP server's launch command from PUBLIC install
  metadata (~/.claude/plugins/installed_plugins.json + the target's
  .claude-plugin/plugin.json), or from an explicit user-maintained
  mcp-suites/targets.yaml, or an explicit --server flag. It never reads the
  target's source. Document the resolution chain and the trust placed in those
  manifests.

  ## Token / credential handling
  Suites declare `env_passthrough: [NAME, ...]`. The runner copies ONLY those
  named host env vars into the spawned child process. Token VALUES never land
  in suite files, reports, or logs (redacted). Document this contract.
-->
