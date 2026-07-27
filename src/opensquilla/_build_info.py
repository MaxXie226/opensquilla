"""Build-time artifact identity.

Standard source checkouts keep this value at ``None``. The Hatch wheel hook
replaces this module inside a built wheel when the build environment supplies
``OPENSQUILLA_BUILD_COMMIT`` or ``GITHUB_SHA``.
"""

from __future__ import annotations

BUILD_COMMIT: str | None = None

__all__ = ["BUILD_COMMIT"]
