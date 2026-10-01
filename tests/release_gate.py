"""The release gate: OPENEVP_RELEASE_GATE=1 (set by build_windows.ps1 and
tools/release_check.py) turns "skipped: the decoder's tables or C core are
missing" into a failure.

The decoders' golden tests (test vectors, the C cores against pure Python, the
marks fingerprint of decoded audio) need the git-ignored table data of both
Sony decoders (LPEC LP and SP, and LPEC ST) and the built lpec_core.dll and
lpec_st_core.dll. On a dev checkout without them those tests skip; a
release must never be built from such a checkout, nor from one whose tables
or DLL are damaged (both load as "not available" and would skip as well).

- ``require(ok, reason)`` replaces ``unittest.skipUnless(ok, reason)``.
- ``skip_or_fail(reason)`` replaces ``self.skipTest(reason)`` / ``raise unittest.SkipTest(reason)``.
- ``decoder_problem()`` says what is missing or damaged, or None.
"""
import functools
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

ENABLED = os.environ.get("OPENEVP_RELEASE_GATE") == "1"
FLAG = "OPENEVP_RELEASE_GATE=1"


def _gate_message(reason):
    return f"release gate ({FLAG}): a decoder test cannot run: {reason}"


def require(ok, reason):
    """unittest.skipUnless(ok, reason), except that in the release gate an
    unmet requirement fails the test (or every test of a class) instead."""
    if ok or not ENABLED:
        return unittest.skipUnless(ok, reason)
    message = _gate_message(reason)

    def decorate(obj):
        if isinstance(obj, type):
            def setUp(self):
                self.fail(message)
            obj.setUp = setUp
            return obj

        @functools.wraps(obj)
        def failing(self, *args, **kwargs):
            self.fail(message)
        return failing
    return decorate


def skip_or_fail(reason):
    """Raise unittest.SkipTest(reason), or in the release gate a failure (an
    AssertionError). For a test method or a setUpClass."""
    if ENABLED:
        raise AssertionError(_gate_message(reason))
    raise unittest.SkipTest(reason)


def _load_problem(tables, **kwargs):
    try:
        tables.load(**kwargs)
    except tables.TablesMissing as e:          # TablesInvalid (damaged) is a TablesMissing
        return f"{e} ({e.hint})"
    return None


def _dll_problem(core, build="python tools/build_lpec_core.py"):
    if core.available():
        return None
    if not os.path.isfile(core.DLL_PATH):
        return f"{core.DLL_PATH} is not built ({build})"
    return f"{core.DLL_PATH} could not be loaded or has the wrong interface"


def tables_problem():
    """Why the LPEC (LP) table data can't be loaded (missing or damaged), or None."""
    from openevp.decoders.sony_lpec import tables
    return _load_problem(tables)


def sp_tables_problem():
    """Why the LPEC SP (16 kHz) table data can't be loaded (missing or damaged), or None."""
    from openevp.decoders.sony_lpec import config, tables
    return _load_problem(tables, config=config.SP)


def core_problem():
    """Why lpec_core.dll can't be used (not built, or fails to load), or None."""
    from openevp.decoders.sony_lpec import _core
    return _dll_problem(_core)


def st_tables_problem():
    """Why the LPEC ST table data can't be loaded (missing or damaged), or None."""
    from openevp.decoders.sony_lpec_st import tables
    return _load_problem(tables)


def st_core_problem():
    """Why lpec_st_core.dll can't be used (not built, or fails to load), or None."""
    from openevp.decoders.sony_lpec_st import _core
    return _dll_problem(_core)


def mp3_core_problem():
    """Why mp3_core.dll (the MP3 decoder, minimp3) can't be used, or None. It has no
    pure-Python fallback: without it MP3 files cannot be played at all."""
    from openevp.decoders.mp3 import _core
    return _dll_problem(_core)


def decoder_problem():
    """What keeps the decoders' golden tests (LPEC LP, SP and ST, MP3) from running, or None."""
    problems = [p for p in (tables_problem(), sp_tables_problem(), core_problem(), st_tables_problem(),
                            st_core_problem(), mp3_core_problem()) if p]
    return "; ".join(problems) or None
