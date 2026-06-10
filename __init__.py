"""Hermes directory-plugin entry point (git clone / ~/.hermes/plugins/pingram).

Hermes imports this file as ``hermes_plugins.pingram``, not as a top-level
``pingram_gateway`` package, so ensure the plugin root is on ``sys.path`` before
importing the real package (same effect as the old flat ``pingram_gateway = "."``
layout in pyproject.toml).
"""
import sys
from pathlib import Path

_plugin_root = Path(__file__).resolve().parent
_root = str(_plugin_root)
if _root not in sys.path:
    sys.path.insert(0, _root)

from pingram_gateway.register import register

__all__ = ["register"]
