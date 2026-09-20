"""Phase 9 web UI package: Jinja2 templates + HTMX views.

Shipping the browser interface as part of the Python package keeps template
and static assets self-contained for the multi-arch container build.
"""

from buku.web.templates import STATIC_DIR

__all__ = ["STATIC_DIR"]
