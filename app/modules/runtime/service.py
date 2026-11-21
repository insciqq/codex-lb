from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime

import aiohttp

from app import __version__
from app.core.utils.time import utcnow
from app.modules.runtime.release_version import ReleaseVersion, parse_release_tag, parse_running_version
from app.modules.runtime.schemas import RuntimeVersionResponse

logger = logging.getLogger(__name__)

_GITHUB_LATEST_RELEASE_URL = "https://api.github.com/repos/insciqq/codex-lb/releases/latest"
_LATEST_RELEASE_PAGE_URL = "https://github.com/insciqq/codex-lb/releases/latest"


@dataclass(frozen=True, slots=True)
class _RuntimeVersionSnapshot:
    current_version: str
    latest_version: str | None
    update_available: bool
    checked_at: datetime
    source: str | None


class RuntimeVersionService:
    def __init__(
        self,
        *,
        current_version: str = __version__,
        ttl_seconds: float = 6 * 60 * 60,
        failure_ttl_seconds: float = 15 * 60,
        github_token_env_var: str = "GITHUB_TOKEN",
    ) -> None:
        self._current_version = current_version
        self._ttl_seconds = ttl_seconds
        self._failure_ttl_seconds = failure_ttl_seconds
        self._github_token_env_var = github_token_env_var
        self._cached_snapshot: _RuntimeVersionSnapshot | None = None
        self._cached_at = 0.0
        self._lock = asyncio.Lock()

    async def get_version_status(self) -> RuntimeVersionResponse:
        now = time.monotonic()
        if self._cached_snapshot is not None and now - self._cached_at < self._cache_ttl_for(self._cached_snapshot):
            return _to_response(self._cached_snapshot)

        async with self._lock:
            now = time.monotonic()
            if self._cached_snapshot is not None and now - self._cached_at < self._cache_ttl_for(self._cached_snapshot):
                return _to_response(self._cached_snapshot)

            snapshot = await self._fetch_snapshot()
            self._cached_snapshot = snapshot
            self._cached_at = time.monotonic()
            return _to_response(snapshot)

    async def invalidate(self) -> None:
        async with self._lock:
            self._cached_snapshot = None
            self._cached_at = 0.0

    async def _fetch_snapshot(self) -> _RuntimeVersionSnapshot:
        checked_at = utcnow()
        try:
            latest = await self._fetch_latest_release()
        except Exception:
            logger.warning("Failed to fetch latest codex-lb release from GitHub", exc_info=True)
            return _RuntimeVersionSnapshot(
                current_version=self._current_version,
                latest_version=None,
                update_available=False,
                checked_at=checked_at,
                source=None,
            )

        # A running version that is not a release spelling never reports an update.
        current = parse_running_version(self._current_version)
        update_available = current is not None and latest > current
        return _RuntimeVersionSnapshot(
            current_version=self._current_version,
            latest_version=latest.semver,
            update_available=update_available,
            checked_at=checked_at,
            source="github",
        )

    async def _fetch_latest_release(self) -> ReleaseVersion:
        timeout = aiohttp.ClientTimeout(total=10, sock_connect=5, sock_read=5)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": f"codex-lb/{self._current_version}",
        }
        github_token = os.getenv(self._github_token_env_var, "").strip()
        if github_token:
            headers["Authorization"] = f"Bearer {github_token}"
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(_GITHUB_LATEST_RELEASE_URL, headers=headers) as response:
                if response.status != 200:
                    raise RuntimeError(f"GitHub releases API returned HTTP {response.status}")
                data = await response.json()

        raw_version = data.get("tag_name") if isinstance(data, dict) else None
        if not isinstance(raw_version, str):
            raise RuntimeError(f"GitHub release tag_name is not a string: {raw_version!r}")
        release = parse_release_tag(raw_version.strip())
        if release is None:
            raise RuntimeError(f"GitHub release tag_name is not a fork release tag: {raw_version!r}")
        return release

    def _cache_ttl_for(self, snapshot: _RuntimeVersionSnapshot) -> float:
        if snapshot.source is None:
            return self._failure_ttl_seconds
        return self._ttl_seconds


def _to_response(snapshot: _RuntimeVersionSnapshot) -> RuntimeVersionResponse:
    return RuntimeVersionResponse(
        current_version=snapshot.current_version,
        latest_version=snapshot.latest_version,
        update_available=snapshot.update_available,
        checked_at=snapshot.checked_at,
        source=snapshot.source,
        release_url=_LATEST_RELEASE_PAGE_URL,
    )


_runtime_version_service = RuntimeVersionService()


def get_runtime_version_service() -> RuntimeVersionService:
    return _runtime_version_service
