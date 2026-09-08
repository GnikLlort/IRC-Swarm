"""Pytest configuration.

Ensures the package directory is importable and routes all logging to a
temporary directory so the test-suite never writes into ``logs/`` and never
spams the console.
"""

import os
import sys
import tempfile

# Make the package (irc_bot_manager/) importable no matter where pytest runs.
_PKG = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG not in sys.path:
    sys.path.insert(0, _PKG)

import logger as logger_mod  # noqa: E402

_TMP_LOG = tempfile.mkdtemp(prefix="irmgr_test_logs_")
logger_mod.setup(_TMP_LOG, "WARNING")


def pytest_configure(config):
    config.addinivalue_line("markers", "integration: uses the mock IRC server")
