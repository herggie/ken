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

        topic = self.config.ntfy_topic
        if not topic:
            raise ValueError(
                "ntfy topic missing — set NTFY_TOPIC (env / GH secret)"
            )
        if any(c.isspace() for c in topic):
            raise ValueError(
                f"ntfy topic {topic!r} contains spaces — topics must be URL-safe "
                "(letters, digits, - and _ only), e.g. 'starter-changes-9f3k2x'"
            )
        endpoint = f"{self.config.ntfy_server.rstrip('/')}/{topic}"
        # HTTP headers must be latin-1; the Title can't carry emoji, so strip any
        # non-ASCII from it (the emoji-rich content still rides in the body).
        safe_title = title.encode("ascii", "ignore").decode("ascii").strip() or "Roster Health"
        headers = {"Title": safe_title, "Priority": self.config.priority}
        token = os.environ.get("NTFY_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if url:
            headers["Click"] = url
        log.info("ntfy POST -> %s (topic=%s)", endpoint, topic)
        resp = requests.post(endpoint, data=body.encode("utf-8"), headers=headers, timeout=15)
        if resp.status_code >= 400:
            raise RuntimeError(f"ntfy returned HTTP {resp.status_code}: {resp.text[:200]}")
        log.info("ntfy delivered (HTTP %s) to topic %s", resp.status_code, topic)


class GithubNotifier(Notifier):
    """Post the digest as a comment on a GitHub issue or PR.

    Lets a locally-run monitor "report back" into a PR that Claude Code is
    watching (a new comment there wakes the watching session). Needs
    ``GITHUB_TOKEN`` in the env (a fine-grained token with issues:write /
    pull_requests:write on the repo) plus ``notify.github_repo`` and
    ``notify.github_issue`` in config.
    """

    def send(self, title: str, body: str, url: str | None = None) -> None:
        import requests

        token = os.environ.get("GITHUB_TOKEN")
        repo = self.config.github_repo
        issue = self.config.github_issue
        if not (token and repo and issue):
            raise ValueError(
                "github notifier needs GITHUB_TOKEN (env) + notify.github_repo "
                "+ notify.github_issue (config)"
            )
        endpoint = f"https://api.github.com/repos/{repo}/issues/{issue}/comments"
        comment = f"**{title}**\n\n```\n{body}\n```"
        if url:
            comment += f"\n\n[sources]({url})"
        comment += "\n\n_posted automatically by the roster-health monitor_"
        resp = requests.post(
            endpoint,
            json={"body": comment},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        log.info("posted digest to %s#%s", repo, issue)


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
    "github": GithubNotifier,
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
