# agent-mcp-tester

End-to-end tests any MCP server from inside Claude Code — record once with an LLM sweep, replay forever with zero tokens.

**What it does:**

- Record repeatable test suites against any MCP server via an LLM-driven sweep tool (`sweep_mcp`)
- Replay recorded suites deterministically with no LLM calls — safe for CI and repeated regression checks
- Validate suite files for structural correctness before committing them to a repo
- List and manage suites from both MCP tool calls (in-harness skills) and a plain CLI (CI pipelines)
- Ships as a self-contained binary — no Python, no pip, works on Windows and Linux without setup
- Dual-mode server: exposes MCP tools for in-harness use and a CLI subcommand surface for headless CI
