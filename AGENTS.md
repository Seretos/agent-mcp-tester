# agent-mcp-tester

E2E-tests other MCP servers. Two phases: an **LLM sweep** that can *record* repeatable suites, and a **deterministic runner** that *replays* them over raw MCP stdio with zero LLM. PyInstaller-frozen Python, shipped as a self-contained binary (`bin/mcp-tester` on Linux, `bin/mcp-tester.exe` on Windows).

## Why this plugin ships an MCP server (not just a CLI)

The binary is the deterministic runner. It is **dual-mode**: no args → FastMCP stdio server; a subcommand (`run` / `list` / `validate` / `save` / `serve`) → a plain CLI for CI. Both surfaces call the same core in `runner` / `suites`.

The MCP server is **load-bearing**, not template residue: a skill's `Bash` environment does **not** receive `${CLAUDE_PLUGIN_ROOT}` (it's exported only to MCP-server and hook subprocesses; skills get only `${CLAUDE_SKILL_DIR}`, from which reaching `bin/` is a fragile `../../` hack — anthropics/claude-code#48230). So the in-harness skills have no reliable way to locate and run the binary themselves. The MCP tools (`run_suite`, `list_suites`, `validate_suite`, `save_suite`) are that bridge — the host launches the server with the right path and env. **CLI lane = CI / headless; MCP lane = in-harness skills.** Same engine behind both.

## Design contracts an agent won't infer from the source

- **Source-free target resolution.** The runner spawns the MCP-under-test by reading only *public* launch metadata — a `--server` override, `mcp-suites/targets.yaml`, the local marketplace manifest, or the installed-plugins cache (`resolve.py`'s 4-tier chain). It never reads the target MCP's source, and neither do the LLM agents. Keep it that way: the test engines treat every target as a black box.
- **Suites live in the host repo, not here.** The runner reads/writes `<host-repo-root>/mcp-suites/` (in practice the `mcp-test` workspace), deliberately separate from both this plugin and each target MCP's repo. Suites are authored once (LLM record) and replayed forever (zero LLM).
- **The skills + agent are project-agnostic.** `skills/{sweep-mcp,replay-suite,file-findings}` and `agents/cluster-tester` carry no target-specific knowledge (which MCPs exist, wrapper↔lib pairings, per-MCP hazards, sandbox projects). That all lives in the **host repo's `AGENTS.md`** and the suites/targets the user supplies. Don't bake ecosystem specifics back into these assets.
- **Frozen-spawns-frozen env scrub.** The runner (a one-file PyInstaller binary) spawns *other* one-file PyInstaller binaries. `runner._child_env()` must strip `_MEIPASS2` / `_PYI_*` and reset `LD_LIBRARY_PATH`, or the child's bootloader mis-resolves its libs. This is the highest-risk runtime path — keep it covered by the cross-binary integration test.

## Contracts an agent won't infer from the tree

- **Release is orphan-branch + marketplace dispatch.** `release.yml` (manual: Actions → release → `version=X.Y.Z`) stamps the version, matrix-builds per OS, then force-pushes an orphan `release` branch holding only install-ready files and POSTs a dispatch to `Seretos/agent-marketplace`. `main` and `release` share no history — never merge between them. Clients install at the tag `agent-mcp-tester--vX.Y.Z`.
- **Version is pipeline-owned.** The `version` in `pyproject.toml` and both manifests is a placeholder; the workflow input is the source of truth and the stamp never lands on `main`. Don't hand-bump it.
- **Two host manifests, no `.mcp.json`.** `.claude-plugin/plugin.json` resolves `command` via `${CLAUDE_PLUGIN_ROOT}`, `.codex-plugin/plugin.json` via `${PLUGIN_ROOT}`; both carry an inline `mcpServers` block. Keep them in sync.
- **Required secret:** `MARKETPLACE_DISPATCH_TOKEN` — fine-grained PAT, `Contents: RW` + `Pull requests: RW` on `Seretos/agent-marketplace` only.

## Gotchas (the "why" behind the code)

- **`build.ps1` runs under Windows PowerShell 5.1, PS7, and Linux `pwsh`.** It derives `$IsWindows` from `$env:OS` (5.1 lacks the auto variable) and sets no global `$ErrorActionPreference='Stop'` (PyInstaller floods stderr, which 5.1 wraps as ErrorRecords). The smoke step gates the build on a real MCP `initialize` handshake.
- **`mcp-tester.spec` hidden imports.** The runner is itself an MCP *client* and parses YAML suites, so the spec lists `mcp.client.*`, `yaml`, and `jsonpath_ng` in `extra_hidden` — PyInstaller won't find these from static analysis of a client that imports them lazily.
- **`_resolve_exe` prefers `.exe` on Windows.** A dev `bin/` can hold both the extensionless Linux ELF and the `.exe`; on Windows the ELF would `exists()` first and fail to run as a Win32 image, so resolution explicitly prefers the `.exe`.

## OS targets

Multi-OS (`[windows, linux]`) — the runner is OS-generic and meant to run in Linux CI too. No Win32 bindings, so don't flip to Windows-only.
