# agent-mcp-tester

E2E-tests MCP servers: LLM-driven sweeps that record repeatable test suites, plus a deterministic runner that replays them with zero LLM tokens.

## Quick install

**Claude Code:**

```
/plugin marketplace add Seretos/agent-marketplace
/plugin install agent-mcp-tester@agent-marketplace
```

Self-contained binary — no Python, no `pip install`, no dependencies. The release zip ships native binaries for both Windows (`mcp-tester.exe`) and Linux (`mcp-tester`); the host OS auto-selects the right one.

## Alternative installs

### From the GitHub Releases page

1. Download `agent-mcp-tester-<version>.zip` from [Releases](https://github.com/Seretos/agent-mcp-tester/releases).
2. Unpack to a stable folder (e.g. `C:\Users\<you>\.claude\plugins\agent-mcp-tester\` on Windows, `~/.claude/plugins/agent-mcp-tester/` on Linux).
3. In Claude Code:
   ```
   /plugin install <path-to-unpacked-folder>
   ```

### From the release branch

The `release` branch always carries the latest install-ready files (no zip step):

```
git clone --branch release --depth 1 https://github.com/Seretos/agent-mcp-tester.git
```

Then `/plugin install <cloned-path>` in Claude Code.

### Build from source

Requires Python 3.11+ (standard python.org installer with the `py` launcher on Windows; `python3` on Linux).

```powershell
git clone https://github.com/Seretos/agent-mcp-tester.git
cd agent-mcp-tester
pwsh -File scripts/build.ps1 -Clean -Package
```

Output on Windows: `bin/mcp-tester.exe`. On Linux: `bin/mcp-tester`. Then install via `/plugin install <path>`.
