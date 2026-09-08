"""Pluggable notification.

Providers: stub (prints), ntfy, pushover, discord, slack. Only the stub and
ntfy are wired; the rest raise a clear NotImplementedError so the config
surface is visible. Credentials come from env, never config.yaml.

The *decision* to send lives in monitor.py (fire only on change). A notifier
just delivers a title + body.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

from models import NotifyConfig

log = logging.getLogger("roster-health.notify")


class Notifier(ABC):
    def __init__(self, config: NotifyConfig):
        self.config = config

    @abstractmethod
    def send(self, title: str, body: str, url: str | None = None) -> None: ...


class StubNotifier(Notifier):
    """Logs what *would* be sent. Used for --dry-run and as the safe default."""

    def send(self, title: str, body: str, url: str | None = None) -> None:
        log.info("[stub notify] %s\n%s", title, body)
        print("\n=== NOTIFICATION (stub, not sent) ===")
        print(title)
        print(body)
        if url:
            print(url)
        print("=== end notification ===")


class NtfyNotifier(Notifier):
    def send(self, title: str, body: str, url: str | None = None) -> None:
        import requests

        if not self.config.ntfy_topic:
            raise ValueError("notify.ntfy_topic is required for the ntfy provider")
        endpoint = f"{self.config.ntfy_server.rstrip('/')}/{self.config.ntfy_topic}"
        headers = {"Title": title, "Priority": self.config.priority}
        token = os.environ.get("NTFY_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if url:
            headers["Click"] = url
        resp = requests.post(endpoint, data=body.encode("utf-8"), headers=headers, timeout=15)
        resp.raise_for_status()
        log.info("ntfy delivered to %s", endpoint)


class _NotImplementedNotifier(Notifier):
    provider = "?"

    def send(self, title: str, body: str, url: str | None = None) -> None:
        raise NotImplementedError(
            f"notify provider '{self.provider}' not implemented yet; "
            "use 'ntfy' or 'stub' for now"
        )


class PushoverNotifier(_NotImplementedNotifier):
    provider = "pushover"


class DiscordNotifier(_NotImplementedNotifier):
    provider = "discord"


class SlackNotifier(_NotImplementedNotifier):
    provider = "slack"


_PROVIDERS = {
    "stub": StubNotifier,
    "ntfy": NtfyNotifier,
    "pushover": PushoverNotifier,
    "discord": DiscordNotifier,
    "slack": SlackNotifier,
}


def make_notifier(config: NotifyConfig, *, dry_run: bool) -> Notifier:
    if dry_run:
        return StubNotifier(config)
    cls = _PROVIDERS.get(config.provider.lower())
    if cls is None:
        raise ValueError(
            f"unknown notify provider '{config.provider}'; "
            f"choose one of {sorted(_PROVIDERS)}"
        )
    return cls(config)
