#!/usr/bin/env python3
"""
Shared GUI utilities for FreeD Dashboard and simulators.
Provides platform-aware font selection and Windows console configuration.
"""

import os
import sys

# Platform-aware monospace / sans-serif font names
if sys.platform == 'darwin':
    FONT_MONO = 'Menlo'
    FONT_SANS = 'SF Pro Text'
elif sys.platform == 'win32':
    FONT_MONO = 'Consolas'
    FONT_SANS = 'Segoe UI'
else:                            # Linux / other
    FONT_MONO = 'DejaVu Sans Mono'
    FONT_SANS = 'DejaVu Sans'


def configure_stdout() -> None:
    """Configure UTF-8 stdout/stderr on Windows; redirect to devnull in --noconsole mode.

    In windowed (--noconsole) mode stdout/stderr are None.  Any print() call in a
    background thread would raise AttributeError and silently kill that thread.
    Redirecting to devnull prevents that without requiring guards throughout the code.
    """
    if sys.platform == 'win32' and 'pytest' not in sys.modules:
        try:
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
        except (AttributeError, TypeError):
            _devnull = open(os.devnull, 'w', encoding='utf-8')
            sys.stdout = _devnull
            sys.stderr = _devnull
