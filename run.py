#!/usr/bin/env python3
"""Project-root entry point; makes the src-layout package runnable in place."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
runpy.run_module("nerveforge", run_name="__main__")
