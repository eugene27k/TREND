"""Read-only HTTP API behind the dashboard (US-T18, US-T19).

The engine is the only writer of its SQLite file; this package opens those
files read-only and answers every page from what is already stored.
"""

from aegis.api.app import create_app

__all__ = ["create_app"]
