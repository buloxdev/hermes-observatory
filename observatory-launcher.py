#!/usr/bin/env python3
"""Standalone launcher for Observatory TUI."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

_NS_PARENT = "hermes_plugins"

if _NS_PARENT not in sys.modules:
    ns_pkg = types.ModuleType(_NS_PARENT)
    ns_pkg.__path__ = []
    ns_pkg.__package__ = _NS_PARENT
    sys.modules[_NS_PARENT] = ns_pkg

HERE = Path(__file__).resolve().parent
if HERE.name != "hermes-observatory":
    print("ERROR: launcher must be inside hermes-observatory/", file=sys.stderr)
    sys.exit(1)

key = "hermes-observatory"
slug = key.replace("/", "__").replace("-", "_")
module_name = f"{_NS_PARENT}.{slug}"

init_file = HERE / "__init__.py"
spec = importlib.util.spec_from_file_location(
    module_name,
    init_file,
    submodule_search_locations=[str(HERE)],
)
mod = importlib.util.module_from_spec(spec)
sys.modules[module_name] = mod

try:
    spec.loader.exec_module(mod)
except ImportError as e:
    print(f"ERROR import plugin: {e}", file=sys.stderr)
    print(f"  Install deps with: {Path.home()}/.hermes/hermes-agent/venv/bin/python3 -m pip install textual", file=sys.stderr)
    sys.exit(1)

try:
    app = mod.tui.ObservatoryApp()
except Exception as e:
    print(f"ERROR creating app: {e}", file=sys.stderr)
    import traceback; traceback.print_exc()
    sys.exit(1)

try:
    app.run()
except (KeyboardInterrupt, SystemExit):
    pass
