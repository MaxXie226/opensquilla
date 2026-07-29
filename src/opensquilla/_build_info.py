"""Build-time artifact identity.

Standard source checkouts keep both values at ``None``. The Hatch wheel hook
replaces this module inside every standard wheel so runtime behavior does not
depend on files left behind by an older installation.
"""

from __future__ import annotations

BUILD_COMMIT: str | None = None
BUILD_UI_MODE: str | None = None

__all__ = ["BUILD_COMMIT", "BUILD_UI_MODE"]
