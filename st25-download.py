#!/usr/bin/env python3
"""Entry point (also the PyInstaller target).

Everything, including imports, runs inside one error boundary so a
double-clicked window never vanishes with only a traceback.
"""
import sys
import traceback


def _pause():
    # Only for a double-click (no arguments); a command-line run should just exit.
    if (getattr(sys, "frozen", False) and sys.platform == "win32" and len(sys.argv) == 1
            and sys.stdin and sys.stdin.isatty()):
        try:
            input("\nPress Enter to close...")   # keep the window open after a double-click
        except EOFError:
            pass


if __name__ == "__main__":
    code = 1
    try:
        from st25.cli import main
        code = main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except BaseException:
        print("\nUnexpected error - please report it at https://github.com/MBarc/OpenEVP/issues with this text:\n")
        traceback.print_exc()
    finally:
        _pause()
    sys.exit(code)
