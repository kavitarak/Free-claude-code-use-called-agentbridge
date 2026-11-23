"""Calls to the model provider, with key rotation and fallback across models."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import urlparse

from . import __version__
from .config import PROVIDERS, ConfigError, Settings, split_target

# Status codes where trying the next key or the next fallback model makes sense.
RETRYABLE = {404, 408, 409, 425, 429, 500, 502, 503, 504, 529}
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never proxy localhost
_default = urllib.request.build_opener()  # respects HTTPS_PROXY for remote providers


class UpstreamError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Upstream:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.slots = threading.BoundedSemaphore(settings.max_concurrency)
        self._turn: dict[str, int] = {}
        self._lock = threading.Lock()

    def scrub(self, text: str) -> str:
        """Remove every secret we know from text that may reach logs or clients."""
        for secret in self.settings.secrets():
            text = text.replace(secret, "***")
        return text[:600]

    def _keys(self, provider: str) -> list[str]:
        keys = self.settings.keys(provider)
        if len(keys) <= 1:
            return keys
        with self._lock:  # round robin, so load spreads over several keys
            start = self._turn.get(provider, 0)
            self._turn[provider] = start + 1
        return [keys[(start + i) % len(keys)] for i in range(len(keys))]

    def _post(self, url: str, key: str, body: dict):
        headers = {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if body.get("stream") else "application/json",
            "User-Agent": f"agentbridge/{__version__}",
        }
        if key:
            headers["Authorization"] = f"Bearer {key}"
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        opener = _direct if urlparse(url).hostname in LOCAL_HOSTS else _default
        return opener.open(req, timeout=self.settings.timeout)

    def open(self, chain: list[str], build_body: Callable[[str], dict], path: str = "/chat/completions"):
        """Try each provider/model in order. Returns (http response, target used)."""
        last: UpstreamError | None = None
        for target in chain:
            try:
                provider, model = split_target(target)
            except ConfigError as e:
                last = UpstreamError(500, str(e))
                continue
            p = PROVIDERS[provider]
            base = self.settings.base_url(provider)
            if not base:
                last = UpstreamError(500, f"{p.base_env} is not set")
                continue
            keys = self._keys(provider)
            if not keys:
                last = UpstreamError(401, f"{p.key_env} is not set")
                continue
            body = build_body(model)
            for key in keys:
                for _ in range(2):  # the 2nd pass only retries without stream_options
                    try:
                        return self._post(base + path, key, body), target
                    except urllib.error.HTTPError as e:
                        detail = self.scrub(e.read().decode("utf-8", "replace"))
                        if e.code == 400 and "stream_options" in body and "stream_options" in detail:
                            body = {k: v for k, v in body.items() if k != "stream_options"}
                            continue
                        last = UpstreamError(e.code, f"{target}: {detail or e.reason}")
                        if e.code in (401, 403) or e.code in RETRYABLE:
                            break  # next key, then next model
                        raise last from None
                    except (urllib.error.URLError, TimeoutError, OSError) as e:
                        reason = self.scrub(str(getattr(e, "reason", e)))
                        last = UpstreamError(502, f"{target}: cannot reach {base} ({reason})")
                        break
        raise last or UpstreamError(500, "no model configured")
