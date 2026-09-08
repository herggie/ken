"""Shared plumbing for source adapters: a polite HTTP session with an on-disk
cache, plus a decorator that guarantees an adapter never crashes the run.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from models import Config, SourceResult, SourceTier

log = logging.getLogger("roster-health.sources")


class PoliteSession:
    """Thin wrapper over requests.Session enforcing a real user-agent, a
    minimum gap between requests to the same host, and a JSON GET cache with
    per-call TTL. Kept dependency-light so adapters can share one instance.

    ``requests`` is imported lazily so importing an adapter (e.g. for tests or
    ``--list-sources``) doesn't hard-require the dependency.
    """

    def __init__(self, config: Config):
        self.config = config
        self.user_agent = config.politeness.user_agent
        self.cache_dir = Path(config.politeness.cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_hit: dict[str, float] = {}
        self._session = None  # lazily constructed requests.Session

    # -- lazy requests session ------------------------------------------- #
    def _requests(self):
        if self._session is None:
            import requests  # noqa: WPS433 (intentional lazy import)

            s = requests.Session()
            s.headers.update({"User-Agent": self.user_agent})
            self._session = s
        return self._session

    # -- politeness ------------------------------------------------------ #
    def _throttle(self, host: str, min_interval: float) -> None:
        last = self._last_hit.get(host, 0.0)
        wait = min_interval - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    # -- cache ----------------------------------------------------------- #
    def _cache_path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:16]
        return self.cache_dir / f"{digest}.json"

    def _read_cache(self, key: str, ttl_seconds: float) -> Any | None:
        p = self._cache_path(key)
        if not p.exists():
            return None
        age = time.time() - p.stat().st_mtime
        if age > ttl_seconds:
            return None
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cache(self, key: str, value: Any) -> None:
        try:
            self._cache_path(key).write_text(json.dumps(value))
        except (TypeError, OSError) as exc:
            log.debug("cache write failed for %s: %s", key, exc)

    # -- public GET ------------------------------------------------------ #
    def get_json(
        self,
        url: str,
        *,
        cache_key: str | None = None,
        ttl_minutes: int = 180,
        min_interval: float = 2.0,
        timeout: float = 20.0,
        local_file_env: str | None = None,
    ) -> Any:
        """GET ``url`` and parse JSON, with cache + throttle.

        If ``local_file_env`` names an env var pointing at a JSON file, that
        file is loaded instead of hitting the network. This is how the offline
        Sleeper fixture is wired in (and a handy escape hatch when an upstream
        is firewalled), without the adapter knowing about test plumbing.
        """
        if local_file_env and os.environ.get(local_file_env):
            path = os.environ[local_file_env]
            log.info("loading %s from local file %s", url, path)
            return json.loads(Path(path).read_text())

        key = cache_key or url
        cached = self._read_cache(key, ttl_minutes * 60)
        if cached is not None:
            log.info("cache hit: %s", key)
            return cached

        host = url.split("/")[2] if "//" in url else url
        self._throttle(host, min_interval)
        log.info("GET %s", url)
        resp = self._requests().get(url, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
        self._write_cache(key, data)
        return data

    def get_text(
        self,
        url: str,
        *,
        min_interval: float = 2.0,
        timeout: float = 20.0,
    ) -> str:
        host = url.split("/")[2] if "//" in url else url
        self._throttle(host, min_interval)
        log.info("GET %s", url)
        resp = self._requests().get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.text


def fetch_guard(source: str, tier: SourceTier) -> Callable:
    """Decorator: turn any exception in an adapter's ``fetch`` into a structured
    ``SourceResult.failed`` so one dead source never kills the whole run.
    """

    def decorator(fn: Callable[..., SourceResult]) -> Callable[..., SourceResult]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs) -> SourceResult:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 — deliberate: isolate the source
                log.warning("source %s failed: %s", source, exc)
                return SourceResult.failed(source, tier, f"{type(exc).__name__}: {exc}")

        return wrapper

    return decorator
