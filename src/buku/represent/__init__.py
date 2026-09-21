"""Representation profiles: named renderings of logical books.

Registering the shipped profiles here makes them available to
:func:`buku.represent.base.get_profile` and therefore to the
:class:`~buku.services.representation.RepresentationService` and the OPDS X4
catalog (Phase 16). Importing this package is a side-effect-free registry
setup — no filesystem access, no media writes.
"""

from __future__ import annotations

from buku.represent.base import (
    GenerationResult,
    ProfileNotSupportedError,
    RepresentationError,
    RepresentationProfile,
    UnknownProfileError,
    get_profile,
    profile_names,
    register_profile,
)
from buku.represent.optimizer import OptimizerResult, X4Optimizer
from buku.represent.original import OriginalProfile
from buku.represent.x4 import X4Profile

register_profile(OriginalProfile())
register_profile(X4Profile())

__all__ = [
    "GenerationResult",
    "OptimizerResult",
    "OriginalProfile",
    "ProfileNotSupportedError",
    "RepresentationError",
    "RepresentationProfile",
    "UnknownProfileError",
    "X4Optimizer",
    "X4Profile",
    "get_profile",
    "profile_names",
    "register_profile",
]
