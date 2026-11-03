"""Pytest bootstrap for the PatchworkPDF regression suite.

The backend modules import each other as top-level packages (``from pdf import
...``, ``from bootstrap import ...``), so ``src/backend`` must be on sys.path.
This runs at collection time for pytest; each test module also inserts the same
path itself so the files can be run standalone (``python tests/test_*.py``)
without pytest installed.
"""
import os
import sys

_BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'src', 'backend')
if _BACKEND not in sys.path:
    sys.path.insert(0, os.path.abspath(_BACKEND))
