"""Entry point so `python -m codewu` works in addition to `codewu`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
