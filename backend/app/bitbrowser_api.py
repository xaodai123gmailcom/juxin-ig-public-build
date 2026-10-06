"""Stable BitBrowser Local API service entry point.

Only this module is imported by production services.  The legacy connection stack is
deliberately not part of the runtime path; V2 owns discovery, requests, inventory and
per-window cancellation from one actor.
"""

from .bitbrowser_v2 import BitBrowserClient, validate_profile_id

__all__ = ["BitBrowserClient", "validate_profile_id"]
