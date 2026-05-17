"""CodeWu — a minimal coding agent prototype.

Public API: just `main`, exposed for the console script in pyproject.toml.
"""

__version__ = "0.1.15"

from .cli import main

__all__ = ["main", "__version__"]
