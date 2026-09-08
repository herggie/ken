"""Source adapters.

Each adapter is one file with one job: fetch from a single upstream and return
a :class:`models.SourceResult`. Adapters must never raise to the caller — wrap
your fetch in try/except and return ``SourceResult.failed(...)`` instead. See
``base.fetch_guard`` for the decorator that enforces that.

To add a new source: drop a ``<name>.py`` here exposing
``fetch(config, session) -> SourceResult`` and register it in ``monitor.py``'s
``SOURCES`` table. That is the whole extension surface.
"""
