"""Enables ``python -m mimiry ...`` (notably ``python -m mimiry setup``)."""

import sys

from mimiry._cli import main

if __name__ == "__main__":
    sys.exit(main())
