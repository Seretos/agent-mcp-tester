"""Source-free resolution of a logical server -> a spawnable command.

The deterministic runner must never read the *source* of the MCP under test —
only its PUBLIC launch contract. This module maps a suite's logical server
handle to ``{command, args}`` through a four-tier priority chain:

1. an explicit ``--server name=cmd`` CLI override,
2. a hand-maintained ``mcp-suites/targets.yaml`` mapping,
3. the local dev marketplace at the repo root (``.claude-plugin/marketplace.json``
   -> the plugin's own ``.claude-plugin/plugin.json``) — this is the path that
   matches the mcp-test workspace, where plugins are symlinked, not cached,
4. the global installed-plugins cache (``~/.claude/plugins/installed_plugins.json``
   -> the cached plugin's ``.claude-plugin/plugin.json``).

Every tier reads only install metadata + the published plugin manifest. None of
them, and nothing the LLM agents touch, reads the MCP's implementation.
"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ResolutionError(RuntimeError):
    """A logical server could not be resolved to a launch command."""


@dataclass
class ServerLaunch:
    command: str
    args: list[str] = field(default_factory=list)
    source: str = "unknown"
    plugin: str | None = None
    server: str | None = None


def resolve(
    logical: str,
    spec: dict[str, Any],
    *,
    root: Path,
    overrides: dict[str, str] | None = None,
    targets: dict[str, Any] | None = None,
) -> ServerLaunch:
    """Resolve one logical server from its suite ``spec`` via the priority chain."""
    plugin = spec.get("plugin")
    server = spec.get("server")
    marketplace = spec.get("marketplace")

    # 1. explicit CLI override (highest precedence)
    if overrides and logical in overrides:
        parts = shlex.split(overrides[logical], posix=(os.name != "nt"))
        if not parts:
            raise ResolutionError(f"--server {logical}= is empty")
        return ServerLaunch(
            command=_resolve_exe(parts[0]),
            args=parts[1:],
            source="override",
            plugin=plugin,
            server=server,
        )

    if not plugin:
        raise ResolutionError(
            f"server {logical!r} has no 'plugin' and no --server override"
        )

    # 2. targets.yaml
    launch = _from_targets(targets, plugin, server)
    if launch is not None:
        return launch

    # 3. local dev marketplace at the repo root
    launch = _from_local_marketplace(root, plugin, server)
    if launch is not None:
        return launch

    # 4. global installed-plugins cache
    launch = _from_installed_cache(plugin, server, marketplace, root)
    if launch is not None:
        return launch

    raise ResolutionError(
        f"could not resolve server {logical!r} (plugin={plugin!r}, server={server!r}). "
        f"Add it to mcp-suites/targets.yaml, install it via the marketplace, or pass "
        f"--server {logical}=<command>."
    )


# --------------------------------------------------------------------------
# Tier helpers
# --------------------------------------------------------------------------
def _from_targets(
    targets: dict[str, Any] | None, plugin: str, server: str | None
) -> ServerLaunch | None:
    if not targets or plugin not in targets:
        return None
    servers = targets[plugin] or {}
    key, entry = _pick_server(servers, server, where=f"targets.yaml[{plugin}]")
    command = os.path.expandvars(str(entry["command"]))
    args = [os.path.expandvars(str(a)) for a in entry.get("args", [])]
    return ServerLaunch(
        command=_resolve_exe(command),
        args=args,
        source="targets.yaml",
        plugin=plugin,
        server=key,
    )


def _from_local_marketplace(
    root: Path, plugin: str, server: str | None
) -> ServerLaunch | None:
    mp_path = root / ".claude-plugin" / "marketplace.json"
    if not mp_path.is_file():
        return None
    data = _read_json(mp_path)
    source_rel: str | None = None
    for entry in data.get("plugins", []):
        if entry.get("name") == plugin:
            src = entry.get("source")
            # source may be a string path or an object; we only handle local paths.
            if isinstance(src, str):
                source_rel = src
            elif isinstance(src, dict) and src.get("source") in (None, "directory"):
                source_rel = src.get("path")
            break
    if not source_rel:
        return None
    plugin_dir = (root / source_rel).resolve()
    return _from_plugin_dir(plugin_dir, plugin, server, source="local-marketplace")


def _from_installed_cache(
    plugin: str, server: str | None, marketplace: str | None, root: Path
) -> ServerLaunch | None:
    index_path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not index_path.is_file():
        return None
    data = _read_json(index_path)
    plugins = data.get("plugins", {})

    if marketplace:
        keys = [k for k in plugins if k == f"{plugin}@{marketplace}"]
    else:
        keys = [k for k in plugins if k.split("@", 1)[0] == plugin]
    if not keys:
        return None

    root_norm = _norm(str(root))
    best: dict[str, Any] | None = None
    best_rank: tuple[int, str] = (-1, "")
    for key in keys:
        for rec in plugins[key]:
            project_match = 1 if _norm(rec.get("projectPath", "")) == root_norm else 0
            rank = (project_match, rec.get("lastUpdated", ""))
            if rank > best_rank:
                best_rank = rank
                best = rec
    if best is None:
        return None

    install_path = Path(best["installPath"])
    launch = _from_plugin_dir(
        install_path,
        plugin,
        server,
        source=f"installed-cache:{best.get('version', '?')}",
    )
    return launch


def _from_plugin_dir(
    plugin_dir: Path, plugin: str, server: str | None, *, source: str
) -> ServerLaunch | None:
    pj_path = plugin_dir / ".claude-plugin" / "plugin.json"
    if not pj_path.is_file():
        return None
    pj = _read_json(pj_path)
    servers = pj.get("mcpServers", {})
    if not servers:
        return None
    key, entry = _pick_server(servers, server, where=str(pj_path))
    command = _expand_plugin_root(str(entry.get("command", "")), plugin_dir)
    args = [_expand_plugin_root(str(a), plugin_dir) for a in entry.get("args", [])]
    return ServerLaunch(
        command=_resolve_exe(command),
        args=args,
        source=source,
        plugin=plugin,
        server=key,
    )


# --------------------------------------------------------------------------
# Small utilities
# --------------------------------------------------------------------------
def _pick_server(
    servers: dict[str, Any], server: str | None, *, where: str
) -> tuple[str, dict[str, Any]]:
    if server is not None:
        if server not in servers:
            raise ResolutionError(f"server key {server!r} not found in {where}")
        return server, servers[server]
    if len(servers) == 1:
        only = next(iter(servers.items()))
        return only
    raise ResolutionError(
        f"{where} declares multiple servers {sorted(servers)}; "
        f"set 'server:' in the suite to pick one"
    )


def _expand_plugin_root(value: str, plugin_dir: Path) -> str:
    root = str(plugin_dir)
    value = value.replace("${CLAUDE_PLUGIN_ROOT}", root).replace(
        "${PLUGIN_ROOT}", root
    )
    return os.path.expandvars(value)


def _resolve_exe(command: str) -> str:
    """On Windows, append ``.exe`` when the extensionless path is missing."""
    p = Path(command)
    if p.exists():
        return str(p)
    if os.name == "nt" and p.suffix == "":
        exe = p.with_suffix(".exe")
        if exe.exists():
            return str(exe)
    return command


def _norm(path: str) -> str:
    if not path:
        return ""
    return os.path.normcase(os.path.normpath(path))


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
