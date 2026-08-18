"""
Pytest configuration for the arclasp SDK test suite.

Adds the worktree root to sys.path so that parity tests can import from
``backend.app.services.policy_engine`` directly.  The backend's DB-dependent
module-level imports are mocked at the parity-test file level before the
backend module is loaded.
"""

import pathlib
import sys

# worktree_root/
#   sdk/
#     tests/   ← this file
_WORKTREE_ROOT = str(pathlib.Path(__file__).parent.parent.parent)
if _WORKTREE_ROOT not in sys.path:
    sys.path.insert(0, _WORKTREE_ROOT)
