from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from fpl_engine.data.cache import DiskJsonCache


class FplApiError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, endpoint: str = ""):
        super().__init__(message)
        self.status = status
        self.endpoint = endpoint


@dataclass(frozen=True)
class FetchResult:
    data: Any
    endpoint: str
    source: str
    fetched_at: datetime
    stale: bool = False
    warning: str | None = None


Transport = Callable[[str, float], Any]


class FplApiClient:
    """Official FPL API client with bounded retries and explicit stale fallback."""

    def __init__(
        self,
        base_url: str,
        cache: DiskJsonCache,
        *,
        timeout: float = 20.0,
        retries: int = 2,
        transport: Transport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cache = cache
        self.timeout = timeout
        self.retries = retries
        self.transport = transport or self._http_get_json

    @staticmethod
    def _http_get_json(url: str, timeout: float) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "fpl-ai-selector/0.1 (local data application)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return json.loads(response.read().decode(charset))
        except urllib.error.HTTPError as exc:
            raise FplApiError(
                f"Official FPL API returned HTTP {exc.code} for {url}",
                status=exc.code,
                endpoint=url,
            ) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise FplApiError(
                f"Could not read valid JSON from the official FPL API: {exc}",
                endpoint=url,
            ) from exc

    def get(
        self,
        endpoint: str,
        *,
        ttl: timedelta,
        force: bool = False,
        allow_stale: bool = True,
    ) -> FetchResult:
        endpoint = endpoint.lstrip("/")
        cached = self.cache.get(endpoint)
        if cached and not force and cached.is_fresh(ttl):
            return FetchResult(
                data=cached.data,
                endpoint=endpoint,
                source="cache",
                fetched_at=cached.fetched_at,
            )

        url = f"{self.base_url}/{endpoint}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                data = self.transport(url, self.timeout)
                entry = self.cache.put(endpoint, data)
                return FetchResult(
                    data=data,
                    endpoint=endpoint,
                    source="network",
                    fetched_at=entry.fetched_at,
                )
            except FplApiError as exc:
                last_error = exc
                if exc.status is not None and 400 <= exc.status < 500:
                    break
            except Exception as exc:  # transports supplied by callers may vary
                last_error = exc
            if attempt < self.retries:
                time.sleep((0.2 * (2**attempt)) + random.uniform(0, 0.1))

        if cached and allow_stale:
            return FetchResult(
                data=cached.data,
                endpoint=endpoint,
                source="stale-cache",
                fetched_at=cached.fetched_at,
                stale=True,
                warning=f"Network refresh failed; using cached data: {last_error}",
            )
        if isinstance(last_error, FplApiError):
            raise last_error
        raise FplApiError(f"Official FPL API request failed for {url}: {last_error}")

    def bootstrap(self, *, force: bool = False) -> FetchResult:
        return self.get("bootstrap-static/", ttl=timedelta(hours=6), force=force)

    def fixtures(self, *, force: bool = False) -> FetchResult:
        return self.get("fixtures/", ttl=timedelta(hours=3), force=force)

    def entry(self, team_id: int, *, force: bool = False) -> FetchResult:
        return self.get(f"entry/{team_id}/", ttl=timedelta(hours=1), force=force)

    def entry_history(self, team_id: int, *, force: bool = False) -> FetchResult:
        return self.get(
            f"entry/{team_id}/history/",
            ttl=timedelta(hours=1),
            force=force,
            allow_stale=True,
        )

    def picks(self, team_id: int, gameweek: int, *, force: bool = False) -> FetchResult:
        return self.get(
            f"entry/{team_id}/event/{gameweek}/picks/",
            ttl=timedelta(minutes=15),
            force=force,
            allow_stale=True,
        )

    def event_live(self, gameweek: int, *, force: bool = False) -> FetchResult:
        return self.get(
            f"event/{gameweek}/live/",
            ttl=timedelta(hours=6),
            force=force,
            allow_stale=True,
        )
