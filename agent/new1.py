#!/usr/bin/env python3
"""Launch the original Termux File Agent without changing its behavior.

This entry point intentionally delegates to the original script, so its tools,
settings, data files, and terminal workflow remain the single source of truth.
"""
from pathlib import Path
import runpy


ORIGINAL_AGENT = Path(__file__).resolve().parents[1] / "Termux_file-agent_v11.0.1.py"

if not ORIGINAL_AGENT.is_file():
    raise SystemExit(f"Original agent not found: {ORIGINAL_AGENT}")

runpy.run_path(str(ORIGINAL_AGENT), run_name="__main__")
