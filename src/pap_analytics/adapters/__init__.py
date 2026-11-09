"""Vendor adapters. Public API re-exported from :mod:`pap_analytics.adapters.base`."""

from pap_analytics.adapters.base import (
    Adapter,
    SessionRef,
    UnknownSourceError,
    detect,
    get,
    register,
    registered,
)

__all__ = ["Adapter", "SessionRef", "UnknownSourceError", "detect", "get", "register", "registered"]

from pap_analytics.adapters import resmed as _resmed  # noqa: E402,F401  (registers the adapter)
