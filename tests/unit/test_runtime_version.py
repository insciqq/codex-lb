from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.runtime.service import RuntimeVersionService


def _mock_response(*, status: int = 200, json_data: object = None) -> MagicMock:
    response = MagicMock()
    response.status = status
    response.json = AsyncMock(return_value=json_data)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)
    return response


def _mock_session(response: MagicMock) -> MagicMock:
    session = MagicMock()
    session.get = MagicMock(return_value=response)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    return session


@pytest.mark.asyncio
async def test_runtime_version_reports_newer_fork_release() -> None:
    service = RuntimeVersionService(current_version="1.25.0+fork.1", ttl_seconds=60)
    session = _mock_session(_mock_response(json_data={"tag_name": "v1.25.0-fork.2"}))

    with patch("app.modules.runtime.service.aiohttp.ClientSession", return_value=session):
        status = await service.get_version_status()

    assert status.current_version == "1.25.0+fork.1"
    assert status.latest_version == "1.25.0-fork.2"
    assert status.update_available is True
    assert status.source == "github"
    assert status.release_url == "https://github.com/insciqq/codex-lb/releases/latest"
    assert session.get.call_args.args[0] == "https://api.github.com/repos/insciqq/codex-lb/releases/latest"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("current_version", "latest_tag", "update_available"),
    [
        ("1.25.0+fork.2", "v1.25.0-fork.2", False),
        ("1.25.0+fork.3", "v1.25.0-fork.2", False),
        ("1.25.0+fork.9", "v1.25.1-fork.1", True),
        ("1.25.0+fork.1", "v1.24.9-fork.9", False),
        ("1.25.0+fork.2", "v1.25.0-fork.10", True),
        # A build without the fork suffix compares as fork 0.
        ("1.25.0", "v1.25.0-fork.1", True),
        ("1.26.0", "v1.25.0-fork.4", False),
        # A running version that is not a release spelling never shows a notice.
        ("1.25.0-beta.9", "v1.25.0-fork.2", False),
    ],
)
async def test_runtime_version_compares_fork_numbers(
    current_version: str, latest_tag: str, update_available: bool
) -> None:
    service = RuntimeVersionService(current_version=current_version, ttl_seconds=60)
    session = _mock_session(_mock_response(json_data={"tag_name": latest_tag}))

    with patch("app.modules.runtime.service.aiohttp.ClientSession", return_value=session):
        status = await service.get_version_status()

    assert status.latest_version == latest_tag.removeprefix("v")
    assert status.update_available is update_available


@pytest.mark.asyncio
@pytest.mark.parametrize("latest_tag", ["v1.26.0", "v1.25.0-beta.1", "v1.25.0-fork.0", "latest"])
async def test_runtime_version_ignores_tags_that_are_not_fork_releases(latest_tag: str) -> None:
    service = RuntimeVersionService(current_version="1.25.0+fork.1", ttl_seconds=60)
    session = _mock_session(_mock_response(json_data={"tag_name": latest_tag}))

    with patch("app.modules.runtime.service.aiohttp.ClientSession", return_value=session):
        status = await service.get_version_status()

    assert status.latest_version is None
    assert status.update_available is False
    assert status.source is None


@pytest.mark.asyncio
async def test_runtime_version_degrades_silently_on_github_failure() -> None:
    service = RuntimeVersionService(current_version="1.19.0", ttl_seconds=60)
    session = _mock_session(_mock_response(status=503, json_data=None))

    with patch("app.modules.runtime.service.aiohttp.ClientSession", return_value=session):
        status = await service.get_version_status()

    assert status.current_version == "1.19.0"
    assert status.latest_version is None
    assert status.update_available is False
    assert status.source is None


@pytest.mark.asyncio
async def test_runtime_version_caches_failed_lookup() -> None:
    service = RuntimeVersionService(current_version="1.19.0", ttl_seconds=60)
    session = _mock_session(_mock_response(status=503, json_data=None))

    with patch("app.modules.runtime.service.aiohttp.ClientSession", return_value=session):
        first = await service.get_version_status()
        second = await service.get_version_status()

    assert first.update_available is False
    assert second.update_available is False
    session.get.assert_called_once()


@pytest.mark.asyncio
async def test_runtime_version_uses_github_token_when_available(monkeypatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-secret")
    service = RuntimeVersionService(current_version="1.19.0", ttl_seconds=60)
    session = _mock_session(_mock_response(json_data={"tag_name": "v1.19.0-fork.1"}))

    with patch("app.modules.runtime.service.aiohttp.ClientSession", return_value=session):
        status = await service.get_version_status()

    assert status.update_available is True
    headers = session.get.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer github-secret"


@pytest.mark.asyncio
async def test_runtime_version_omits_github_token_when_unavailable(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    service = RuntimeVersionService(current_version="1.19.0", ttl_seconds=60)
    session = _mock_session(_mock_response(json_data={"tag_name": "v1.19.0-fork.1"}))

    with patch("app.modules.runtime.service.aiohttp.ClientSession", return_value=session):
        await service.get_version_status()

    headers = session.get.call_args.kwargs["headers"]
    assert "Authorization" not in headers


@pytest.mark.asyncio
async def test_runtime_version_refetches_failed_lookup_after_failure_ttl() -> None:
    service = RuntimeVersionService(current_version="1.19.0", ttl_seconds=60, failure_ttl_seconds=1)
    failed_session = _mock_session(_mock_response(status=503, json_data=None))

    with patch("app.modules.runtime.service.aiohttp.ClientSession", return_value=failed_session):
        failed = await service.get_version_status()

    assert failed.update_available is False
    service._cached_at = time.monotonic() - 2

    successful_session = _mock_session(_mock_response(json_data={"tag_name": "v1.19.0-fork.1"}))
    with patch("app.modules.runtime.service.aiohttp.ClientSession", return_value=successful_session):
        recovered = await service.get_version_status()

    assert recovered.latest_version == "1.19.0-fork.1"
    assert recovered.update_available is True
