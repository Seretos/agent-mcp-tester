# agent-mcp-tester

MCP server that ships as a self-contained binary (PyInstaller-frozen Python). End users don't need a Python toolchain.

## Layout

```
src/mcp_tester_plugin/          # Python source (src-layout)
  server.py                       # FastMCP entry point, wires the tools
  __main__.py                     # python -m / PyInstaller entry

tests/                          # pytest, runs on every push (test.yml matrix: windows-latest + ubuntu-22.04)
scripts/build.ps1               # cross-platform pwsh: PyInstaller wrapper + smoke test + staging
mcp-tester.spec               # PyInstaller config (output extension picked by host OS)
pyproject.toml                  # setuptools (package-dir = src/) + pytest config
.claude-plugin/plugin.json      # Claude manifest; extensionless bin/mcp-tester via ${CLAUDE_PLUGIN_ROOT}
.codex-plugin/plugin.json       # Codex manifest; same surface, command via ${PLUGIN_ROOT}
SECURITY.md                     # threat model — extend per tool surface

.github/workflows/
  test.yml                      # pytest matrix on every push and PR
  release.yml                   # manual-dispatch multi-OS release (stamp -> matrix-build -> assemble)
  dispatch.yml                  # manual recovery: re-send marketplace dispatch
```

## OS_TARGETS

Default: `[windows, linux]`. The pipeline produces native binaries for both platforms and ships them inside a single release zip.

- Both manifests use an extensionless `command` of `bin/mcp-tester` — `${CLAUDE_PLUGIN_ROOT}/bin/mcp-tester` in `.claude-plugin`, `${PLUGIN_ROOT}/bin/mcp-tester` in `.codex-plugin`. On Windows the host resolves that to `mcp-tester.exe`; on Linux to `mcp-tester`. One zip serves both platforms and both hosts.
- `release.yml` is a three-stage pipeline:
  1. **stamp** (Linux) — writes the version into `pyproject.toml` + both plugin manifests (`.claude-plugin/plugin.json` and `.codex-plugin/plugin.json`) and uploads them as an artifact so every downstream job pulls the same stamped sources.
  2. **build** (matrix `windows-latest` + `ubuntu-22.04`) — each runner calls `scripts/build.ps1 -Clean -Package` and uploads its `bin/` payload as `bin-<os>`.
  3. **assemble** (Linux) — merges the per-OS bins into a single `build/stage/agent-mcp-tester/bin/` tree, builds the release zip with correct Unix mode bits via Python's `zipfile`, force-pushes the orphan `release` branch, creates the GitHub Release, and dispatches to the marketplace.

### Windows-only override

If this plugin is genuinely Win32-bound (e.g. depends on `pyvda`, `pywin32`, `comtypes` for COM, like agent-vdesktop), reduce `OS_TARGETS` to `[windows]`:

1. In `.github/workflows/release.yml`:
   - Remove the `ubuntu-22.04` row from the `build` job's `matrix.include`.
   - In the assembly job's "Build merged staging tree" step, drop the `bin/mcp-tester` (Linux binary) assertion and the `chmod +x` on it.
   - In "Push orphan release branch", drop `bin/mcp-tester` from the `git add` / `git update-index --chmod=+x` calls.
2. In `.github/workflows/test.yml`: drop `ubuntu-22.04` from the `matrix.os` list.
3. Append `.exe` to the `command` in **both** manifests: `.claude-plugin/plugin.json` → `${CLAUDE_PLUGIN_ROOT}/bin/mcp-tester.exe`, `.codex-plugin/plugin.json` → `${PLUGIN_ROOT}/bin/mcp-tester.exe`.

The multi-OS shape is the default because most MCP servers are I/O- and HTTP-bound and have no native-Windows dependency. Windows-only is the deliberate exception, not the rule.

## Branches

- `main` — source of truth. All edits go here.
- `release` — orphan branch, force-pushed by `release.yml`. Contains only install-ready files: `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `bin/mcp-tester.exe`, `bin/mcp-tester`, `skills/`, `agents/`, `README.md`. Clients clone at the version tag (e.g. `agent-mcp-tester--v0.0.1`).

The release branch shares no history with main. Don't try to merge between them.

## Release flow

Triggered manually:

```
Actions → release → Run workflow → version=X.Y.Z
```

or `gh workflow run release.yml -f version=X.Y.Z`.

The workflow:
1. Validates `X.Y.Z` is semver.
2. Fails if tag `agent-mcp-tester--vX.Y.Z` already exists.
3. Stamps the version into `pyproject.toml` and `.claude-plugin/plugin.json` (CI checkout only — never pushed back to main).
4. Builds binaries on each OS in the matrix and uploads them as `bin-<os>` artifacts.
5. Assembly job merges every per-OS payload, packs the release zip with Unix-mode bits, force-pushes the orphan `release` branch, creates the GitHub Release with the zip attached, and POSTs to `Seretos/agent-marketplace/dispatches` with the plugin metadata (using `MARKETPLACE_DISPATCH_TOKEN`).

`pyproject.toml`'s `version` field is **not** load-bearing for releases. The workflow input drives everything.

## Required secret

- `MARKETPLACE_DISPATCH_TOKEN` — fine-grained PAT with `Contents: Read and write` + `Pull requests: Read and write` on `Seretos/agent-marketplace` only.

## Build conventions (`scripts/build.ps1`)

- Cross-platform: runs under **Windows PowerShell 5.1**, PowerShell 7 on Windows, AND `pwsh` on Linux. PS 5.1 lacks the auto `$IsWindows` variable so the script derives it from `$env:OS`.
- Output filename: `bin/mcp-tester.exe` on Windows, `bin/mcp-tester` on Linux (no extension). The Linux binary is explicitly `chmod +x`ed after copy.
- No global `$ErrorActionPreference = 'Stop'` — PyInstaller writes heavily to stderr, which PS 5.1 wraps as ErrorRecord and would trip a global Stop.
- Python discovery: prefers `py.exe -3` on Windows locally, falls back to `python` / `python3` (which is what `actions/setup-python` installs).
- The smoke test runs an MCP `initialize` handshake against the freshly built binary. The build fails if the handshake fails.
- `-Package` stages `build/stage/agent-mcp-tester/` with this OS's binary only — `release.yml`'s assembly job merges the per-OS stages into the final zip.

## What this plugin ships beyond the binary

Unlike a plain MCP plugin, `agent-mcp-tester` also ships `skills/` and `agents/`:

- `skills/sweep-mcp` — orchestrates an LLM-driven full-surface sweep; in record mode persists repeatable suites.
- `skills/replay-suite` — deterministically replays committed suites via the runner (zero LLM).
- `skills/file-findings` — files findings/regressions as tickets via the project-issues MCP.
- `agents/cluster-tester` — black-box per-cluster prober spawned by `sweep-mcp`.

The MCP server (the binary) is the **deterministic runner** behind `replay-suite`: it spawns target MCP servers over stdio JSON-RPC and replays recorded suites. See the runner module docstrings under `src/mcp_tester_plugin/`.

## PyInstaller / src-layout notes

- The Python package is `mcp_tester_plugin` under `src/`. `pyproject.toml` declares `package-dir = { "" = "src" }` and `[tool.pytest.ini_options] pythonpath = ["src"]`.
- `mcp-tester.spec` references `src/mcp_tester_plugin/__main__.py` as the entry and `pathex=[ROOT / "src"]`. Adjust both if the layout ever moves.
- The runner is itself an MCP **client** (`mcp.client.stdio`) and parses YAML suites; `mcp-tester.spec` lists `mcp.client.*`, `yaml`, and `jsonpath_ng` in `extra_hidden`.
- It spawns other PyInstaller one-file binaries (the target servers); the spawn helper scrubs `_MEIPASS2` / `_PYI_*` / `LD_LIBRARY_PATH` from the child env so the child's bootloader resolves its own libs.
