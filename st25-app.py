#!/usr/bin/env python3
"""Desktop app entry point (also the PyInstaller target for the app)."""
import sys

from app.main import main

if __name__ == "__main__":
    sys.exit(main())
