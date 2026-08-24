"""codex-resets.com v1 API 客户端。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any

import aiohttp

try:
    from .reset_service import StatusSnapshot
except ImportError:  # AstrBot 按文件路径加载 main.py 时的兼容路径
    from reset_service import StatusSnapshot  # type: ignore


DEFAULT_STATUS_URL = "https://codex-resets.com/api/v1/status"


class ResetApiError(RuntimeError):
    """API 返回异常或响应格式无法解析。"""


@dataclass(frozen=True)
class FetchResult:
    snapshot: StatusSnapshot | None
    not_modified: bool = False
    retry_after: float | None = None


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            from datetime import datetime, timezone

            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None


class CodexResetsClient:
    """带 ETag 缓存和错误退避信息的异步客户端。"""

    def __init__(self, status_url: str = DEFAULT_STATUS_URL, timeout_seconds: int = 15) -> None:
        self.status_url = status_url.strip() or DEFAULT_STATUS_URL
        self.timeout_seconds = max(3, int(timeout_seconds or 15))
        self._session: aiohttp.ClientSession | None = None
        self._etag: str | None = None
        self._last_snapshot: StatusSnapshot | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            self._session = aiohttp.ClientSession(
                timeout=timeout,
                trust_env=True,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "astrbot-plugin-codex-reset/1.0",
                },
            )
        return self._session

    async def fetch_status(self) -> FetchResult:
        session = await self._get_session()
        headers: dict[str, str] = {}
        if self._etag:
            headers["If-None-Match"] = self._etag
        try:
            async with session.get(self.status_url, headers=headers) as response:
                if response.status == 304:
                    if self._last_snapshot is None:
                        # 代理可能错误地复用了旧 ETag；清掉它，下一轮
                        # 重新取 200。
                        self._etag = None
                        return FetchResult(snapshot=None)
                    return FetchResult(
                        snapshot=self._last_snapshot,
                        not_modified=True,
                    )
                if response.status == 429:
                    retry_after = _retry_after(response.headers.get("Retry-After"))
                    return FetchResult(
                        snapshot=None,
                        # 没有 Retry-After 时也主动放慢，避免公共 API 被
                        # 连续重试。
                        retry_after=300.0 if retry_after is None else retry_after,
                    )
                if response.status in {500, 502, 503, 504}:
                    raise ResetApiError(f"API 暂时不可用（HTTP {response.status}）")
                if response.status != 200:
                    body = (await response.text())[:200]
                    raise ResetApiError(f"API 返回 HTTP {response.status}: {body}")
                response_etag = response.headers.get("ETag")
                try:
                    payload: Any = await response.json(content_type=None)
                except (ValueError, TypeError) as exc:
                    raise ResetApiError("API 返回的不是有效 JSON") from exc
                if not isinstance(payload, dict):
                    raise ResetApiError("API 返回格式不是对象")
                data = payload.get("data")
                if not isinstance(data, dict):
                    # 允许反向代理把 v1 的 data 内容提升到顶层。
                    data = payload
                if "latest_reset" not in data and "active_watch" not in data:
                    raise ResetApiError("API 返回缺少重置状态字段")
                snapshot = StatusSnapshot.from_payload(payload)
                self._etag = response_etag or self._etag
                self._last_snapshot = snapshot
                return FetchResult(snapshot=snapshot)
        except asyncio.CancelledError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise ResetApiError(f"请求重置 API 失败：{exc}") from exc

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def invalidate_cache(self) -> None:
        """让下一次轮询重新取得完整快照，避免手动查询吞掉推送变化。"""

        self._etag = None
