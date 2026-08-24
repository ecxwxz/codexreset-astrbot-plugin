"""纯 Python 的 Codex 重置数据模型、解析和中文文案工具。

这个模块不依赖 AstrBot，便于在插件外做单元测试。数据来源是
codex-resets.com 的 v1 公共 API；API 明确区分了「最近公告」和「当前预测」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


WEEKDAYS = (
    "星期一",
    "星期二",
    "星期三",
    "星期四",
    "星期五",
    "星期六",
    "星期日",
)

_BEIJING_ZONE_NAMES = {
    "Asia/Shanghai",
    "Asia/Chongqing",
    "Asia/Harbin",
    "Asia/Kashgar",
    "Asia/Urumqi",
    "PRC",
    "CTT",
}


def parse_datetime(value: Any) -> datetime | None:
    """将 RFC3339/ISO 时间转成带时区的 UTC datetime。"""

    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(raw)
        except ValueError:
            return None
    else:
        return None

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _text(value: Any, default: str = "") -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value).strip()
    return default


@dataclass(frozen=True)
class ResetAnnouncement:
    id: str
    reset_type: str
    announced_at: datetime | None
    text: str
    source_url: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ResetAnnouncement | None":
        if not isinstance(value, Mapping):
            return None
        source = value.get("source")
        source_url = source.get("url", "") if isinstance(source, Mapping) else ""
        return cls(
            id=_text(value.get("id")),
            reset_type=_text(value.get("reset_type"), "regular"),
            announced_at=parse_datetime(value.get("announced_at")),
            text=_text(value.get("text")),
            source_url=_text(source_url),
        )


@dataclass(frozen=True)
class WatchForecast:
    level: str
    chance_percent: int | None
    forecast_window: str
    observed_at: datetime | None
    expires_at: datetime | None
    text: str
    source_url: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "WatchForecast | None":
        if not isinstance(value, Mapping):
            return None
        source = value.get("source")
        source_url = source.get("url", "") if isinstance(source, Mapping) else ""
        chance = value.get("reset_chance_percent")
        if isinstance(chance, bool):
            chance = None
        elif isinstance(chance, (int, float)):
            chance = int(chance)
        else:
            chance = None
        return cls(
            level=_text(value.get("level")),
            chance_percent=chance,
            forecast_window=_text(value.get("forecast_window")),
            observed_at=parse_datetime(value.get("observed_at")),
            expires_at=parse_datetime(value.get("expires_at")),
            text=_text(value.get("text")),
            source_url=_text(source_url),
        )


@dataclass(frozen=True)
class StatusSnapshot:
    latest_reset: ResetAnnouncement | None
    active_watch: WatchForecast | None
    generated_at: datetime | None
    total_resets: int | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "StatusSnapshot":
        """解析 v1 响应，也兼容少数代理把 ``data`` 直接返回的情况。"""

        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            data = payload if isinstance(payload, Mapping) else {}
        stats = data.get("stats")
        total = stats.get("total") if isinstance(stats, Mapping) else None
        if not isinstance(total, int):
            total = None
        meta = payload.get("meta") if isinstance(payload, Mapping) else None
        generated = meta.get("generated_at") if isinstance(meta, Mapping) else None
        return cls(
            latest_reset=ResetAnnouncement.from_mapping(data.get("latest_reset")),
            active_watch=WatchForecast.from_mapping(data.get("active_watch")),
            generated_at=parse_datetime(generated),
            total_resets=total,
        )

    def active_watch_is_valid(self, now: datetime | None = None) -> bool:
        """判断预测窗口是否仍未过期；没有截止时间时按 API 有效看待。"""

        if self.active_watch is None:
            return False
        if self.active_watch.expires_at is None:
            return True
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return self.active_watch.expires_at > current.astimezone(timezone.utc)

    def fingerprint_parts(self) -> dict[str, Any]:
        """返回用于去重的字段，刻意排除 observed_at/generated_at。"""

        watch = self.active_watch
        reset = self.latest_reset
        return {
            "watch": None
            if watch is None
            else {
                "level": watch.level,
                "chance_percent": watch.chance_percent,
                "forecast_window": watch.forecast_window,
                "expires_at": watch.expires_at.isoformat() if watch.expires_at else None,
                # 文本修订通常意味着预测内容发生了变化；观察时间本身
                # 不参与去重。
                "text": watch.text,
                "source_url": watch.source_url,
            },
            "reset": None
            if reset is None
            else {
                "id": reset.id,
                "reset_type": reset.reset_type,
                "announced_at": reset.announced_at.isoformat() if reset.announced_at else None,
                "text": reset.text,
                "source_url": reset.source_url,
            },
        }

    def fingerprint(self) -> str:
        return json.dumps(self.fingerprint_parts(), ensure_ascii=False, sort_keys=True)


def _zone(tz_name: str) -> tzinfo:
    try:
        return ZoneInfo(tz_name or "Asia/Shanghai")
    except (ZoneInfoNotFoundError, ValueError):
        # 精简 Docker/Windows 环境可能没有 tzdata；北京时间没有夏令时，
        # 用固定 UTC+8 比错误地回退到 UTC 更符合用户预期。
        return timezone(timedelta(hours=8), name="Asia/Shanghai")


def timezone_label(tz_name: str) -> str:
    """返回适合消息展示的时区名称。默认/无 tzdata 时显示北京时间。"""

    name = (tz_name or "Asia/Shanghai").strip() or "Asia/Shanghai"
    if name in _BEIJING_ZONE_NAMES:
        return "北京时间"
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return "北京时间"
    return f"{name}时间"


def format_datetime_cn(value: datetime | None, tz_name: str = "Asia/Shanghai") -> str:
    """格式化为类似“8月24号 星期一 早上5点”的中文时间。"""

    parsed = parse_datetime(value)
    if parsed is None:
        return "时间未知"
    local = parsed.astimezone(_zone(tz_name))
    hour = local.hour
    if hour < 5:
        period = "凌晨"
    elif hour < 8:
        period = "早上"
    elif hour < 12:
        period = "上午"
    elif hour < 14:
        period = "中午"
    elif hour < 18:
        period = "下午"
    else:
        period = "晚上"
    display_hour = hour % 12 or 12
    minute = f"{local.minute}分" if local.minute else ""
    return (
        f"{local.month}月{local.day}号 {WEEKDAYS[local.weekday()]} "
        f"{period}{display_hour}点{minute}"
    )


def _reset_type_label(reset_type: str) -> str:
    return {
        "regular": "普通重置",
        "banked": "存入重置额度",
    }.get(reset_type, reset_type or "重置")


def _source_line(url: str, include_source: bool) -> str:
    return f"来源：{url}" if include_source and url else ""


def format_status(
    snapshot: StatusSnapshot,
    *,
    tz_name: str = "Asia/Shanghai",
    include_source: bool = False,
    now: datetime | None = None,
) -> str:
    """生成 ``/reset`` 的完整回复。"""

    lines = ["【ChatGPT重置推送】"]
    tz_label = timezone_label(tz_name)
    watch = snapshot.active_watch if snapshot.active_watch_is_valid(now) else None
    if watch is not None:
        if watch.expires_at is not None:
            when = format_datetime_cn(watch.expires_at, tz_name)
            lines.append(f"预计将在{tz_label} {when} 前后进行。")
        else:
            lines.append("目前有活跃的重置预测，但时间窗口未给出。")
        if watch.chance_percent is not None:
            lines.append(f"社区预测概率：{watch.chance_percent}%。")
        if watch.forecast_window:
            lines.append(f"预测窗口：{watch.forecast_window}。")
        source = _source_line(watch.source_url, include_source)
        if source:
            lines.append(source)
    else:
        lines.append("目前没有可用的下一次重置预测。")

    latest = snapshot.latest_reset
    if latest is not None:
        announced = format_datetime_cn(latest.announced_at, tz_name)
        lines.append(
            "最近一次社区公告："
            f"{tz_label} {announced}（{_reset_type_label(latest.reset_type)}）。"
        )
        source = _source_line(latest.source_url, include_source)
        if source:
            lines.append(source)
    lines.append(
        "提示：以上来自社区追踪/预测，公告时间不等于实际生效时间，"
        "也不是 OpenAI 官方承诺。"
    )
    return "\n".join(lines)


def format_push(
    snapshot: StatusSnapshot,
    *,
    reason: str = "watch",
    tz_name: str = "Asia/Shanghai",
    include_source: bool = False,
    now: datetime | None = None,
) -> str:
    """生成主动推送文案；reason 可为 ``watch``、``reset`` 或 ``both``。"""

    lines = ["【ChatGPT重置推送】"]
    tz_label = timezone_label(tz_name)
    watch = snapshot.active_watch if snapshot.active_watch_is_valid(now) else None
    latest = snapshot.latest_reset
    content_added = False
    if reason in {"watch", "both"} and watch is not None:
        content_added = True
        if watch.expires_at:
            when = format_datetime_cn(watch.expires_at, tz_name)
            lines.append(f"社区预测：重置可能在{tz_label} {when} 前后进行。")
        else:
            lines.append("社区发现了新的重置预测，但暂未给出明确时间窗口。")
        if watch.chance_percent is not None:
            lines.append(f"预测概率：{watch.chance_percent}%。")
        source = _source_line(watch.source_url, include_source)
        if source:
            lines.append(source)
    if reason in {"reset", "both"} and latest is not None:
        content_added = True
        lines.append(
            "社区重置公告："
            f"{_reset_type_label(latest.reset_type)}，"
            f"公告时间为{tz_label} {format_datetime_cn(latest.announced_at, tz_name)}。"
        )
        source = _source_line(latest.source_url, include_source)
        if source:
            lines.append(source)
    if not content_added:
        lines.append("当前没有可用的重置预测或公告变化。")
    lines.append(
        "提示：这是社区追踪/预测，不代表 OpenAI 官方承诺；"
        "公告时间也不一定是实际生效时间。"
    )
    return "\n".join(lines)
