"""Standalone clan extension.

The implementation lives in recruit.py for backwards compatibility with the
first ZIP. Only this extension is loaded by bot.py; recruit.py is not
auto-loaded as a second extension.
"""

from .recruit import setup

__all__ = ["setup"]