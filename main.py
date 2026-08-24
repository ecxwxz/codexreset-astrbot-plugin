"""AstrBot 的 ChatGPT/Codex 重置预测推送插件。"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any, Mapping

try:  # AstrBot >= 4.x
    from astrbot.api import AstrBotConfig, logger
    from astrbot.api.event import AstrMessageEvent, MessageChain, filter
    from astrbot.api.star import Context, Star
    try:
        from astrbot.api.star import StarTools
    except ImportError:  # StarTools 是较新版本 API，下面有路径回退
        StarTools = None  # type: ignore[assignment,misc]
except ImportError as exc:  # 运行插件必须在 AstrBot 环境中
    raise ImportError(
        "astrbot_plugin_codex_reset 必须由 AstrBot 加载，"
        "请不要直接用系统 Python 运行 main.py。"
    ) from exc

try:
    from .reset_api import CodexResetsClient, FetchResult, ResetApiError
    from .reset_service import StatusSnapshot, format_push, format_status
except ImportError:  # AstrBot 有些版本按文件路径加载 main.py
    from reset_api import CodexResetsClient, FetchResult, ResetApiError  # type: ignore
    from reset_service import StatusSnapshot, format_push, format_status  # type: ignore


PLUGIN_NAME = "astrbot_plugin_codex_reset"
DEFAULT_STATUS_URL = "https://codex-resets.com/api/v1/status"


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "是", "开启"}
    if value is None:
        return default
    return bool(value)


def _as_int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    if minimum is not None:
        result = max(minimum, result)
    return result


def _as_targets(value: Any) -> list[str]:
    """兼容 WebUI 的 list、换行文本及旧版 template_list。"""

    values: list[Any]
    if isinstance(value, str):
        values = value.splitlines()
    elif isinstance(value, (list, tuple, set)):
        values = list(value)
    else:
        values = []
    result: list[str] = []
    for item in values:
        if isinstance(item, Mapping):
            item = item.get("umo") or item.get("unified_msg_origin") or item.get("target")
        if not isinstance(item, str):
            continue
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return result


class CodexResetPlugin(Star):
    """轮询社区预测，并向绑定的 QQ/TG 会话主动发送消息。"""

    author = "ctfer"
    name = PLUGIN_NAME

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        # 新版 Star 接受 config；旧版可能只接受 context。
        try:
            super().__init__(context, config)
        except TypeError:
            super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}

        self.status_url = str(
            self._cfg("api_url", DEFAULT_STATUS_URL) or DEFAULT_STATUS_URL
        ).strip()
        self.poll_interval = _as_int(self._cfg("poll_interval_seconds", 120), 120, 60)
        self.timeout_seconds = _as_int(self._cfg("request_timeout_seconds", 15), 15, 3)
        self.timezone = str(self._cfg("timezone", "Asia/Shanghai") or "Asia/Shanghai")
        self.push_on_start = _as_bool(self._cfg("push_on_start", False))
        self.push_watch_updates = _as_bool(self._cfg("push_watch_updates", True), True)
        self.push_reset_announcements = _as_bool(
            self._cfg("push_reset_announcements", True), True
        )
        self.include_source = _as_bool(self._cfg("send_source", False))
        self._targets = _as_targets(self._cfg("targets", []))

        self._client = CodexResetsClient(self.status_url, self.timeout_seconds)
        self._snapshot: StatusSnapshot | None = None
        self._last_parts: dict[str, Any] | None = None
        self._last_success_at: str | None = None
        self._poll_task: asyncio.Task[None] | None = None
        self._fetch_lock = asyncio.Lock()
        # 将 HTTP 获取与快照处理串起来，避免手动 /reset 和后台轮询
        # 交错时让较旧快照覆盖较新的去重状态。
        self._refresh_lock = asyncio.Lock()
        self._process_lock = asyncio.Lock()
        self._target_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._state_loaded = False
        self._data_dir = self._resolve_data_dir()
        self._state_file = self._data_dir / "state.json"

    def _cfg(self, key: str, default: Any) -> Any:
        config = self.config
        if isinstance(config, Mapping):
            return config.get(key, default)
        getter = getattr(config, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                pass
        return default

    def _config_has_key(self, key: str) -> bool:
        """判断配置是否明确提供了某项，区分空列表和没有该项。"""

        config = self.config
        try:
            return key in config  # type: ignore[operator]
        except (TypeError, AttributeError):
            return False

    def _resolve_data_dir(self) -> Path:
        """优先使用新版 StarTools，旧版再退回标准 data/plugin_data 路径。"""

        try:
            if StarTools is None:
                raise RuntimeError("StarTools 不可用")
            data_dir = StarTools.get_data_dir(PLUGIN_NAME)
            path = Path(data_dir)
        except Exception:
            # 仅作为旧版本兼容路径；正常的新版 AstrBot 不会走这里。
            path = Path.cwd() / "data" / "plugin_data" / PLUGIN_NAME
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def _load_state(self) -> None:
        if self._state_loaded:
            return
        state: dict[str, Any] = {}
        get_kv = getattr(self, "get_kv_data", None)
        if callable(get_kv):
            try:
                value = await get_kv("state", {})
                if isinstance(value, dict):
                    state = value
            except Exception as exc:
                logger.debug("读取插件 KV 状态失败，将尝试文件：%s", exc)
        if not state and self._state_file.exists():
            try:
                value = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    state = value
            except (OSError, ValueError) as exc:
                logger.warning("读取重置插件状态失败：%s", exc)

        parts = state.get("last_parts")
        if isinstance(parts, dict):
            self._last_parts = parts
        success_at = state.get("last_success_at")
        if isinstance(success_at, str):
            self._last_success_at = success_at

        # 配置项存在时以配置为准；只有旧版配置没有 targets 字段时，
        # 才恢复文件/KV 中的目标。这样 WebUI 明确清空列表不会被旧状态
        # 复活。
        if not self._targets and not self._config_has_key("targets"):
            saved_targets = _as_targets(state.get("targets", []))
            if saved_targets:
                self._targets = saved_targets
        self._state_loaded = True

    async def _save_state(self, targets: list[str] | None = None) -> None:
        if targets is None:
            async with self._target_lock:
                await self._save_state(targets=list(self._targets))
            return
        async with self._state_lock:
            state = {
                "last_parts": self._last_parts,
                "last_success_at": self._last_success_at,
                "targets": list(targets),
            }
            put_kv = getattr(self, "put_kv_data", None)
            if callable(put_kv):
                try:
                    await put_kv("state", state)
                    return
                except Exception as exc:
                    logger.debug("写入插件 KV 状态失败，将使用文件：%s", exc)
            try:
                self._data_dir.mkdir(parents=True, exist_ok=True)
                temp = self._state_file.with_suffix(".tmp")
                temp.write_text(
                    json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                temp.replace(self._state_file)
            except OSError as exc:
                logger.warning("写入重置插件状态失败：%s", exc)

    async def _save_targets_to_config(self, targets: list[str] | None = None) -> None:
        """将命令订阅同步到 WebUI 配置。"""

        if targets is None:
            async with self._target_lock:
                targets = list(self._targets)

        if isinstance(self.config, dict):
            self.config["targets"] = list(targets)
        else:
            try:
                self.config["targets"] = list(targets)  # type: ignore[index]
            except Exception:
                pass
        saver = getattr(self.config, "save_config", None)
        if callable(saver):
            try:
                result = saver()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:
                logger.debug("保存 WebUI 配置失败：%s", exc)

    async def initialize(self) -> None:
        await self._load_state()
        # 热重载时可能不会再次触发 on_astrbot_loaded；轮询任务自身会先
        # 等待一个短窗口，给 QQ/TG 适配器完成初始化的时间。
        self._start_polling()

    @filter.on_astrbot_loaded()
    async def on_astrbot_loaded(self) -> None:
        # 兼容尚未调用 initialize() 的旧版 AstrBot；有 guard 不会重复启动。
        await self._load_state()
        self._start_polling()

    def _start_polling(self) -> None:
        if self._poll_task is None or self._poll_task.done():
            self._poll_task = asyncio.create_task(self._poll_loop())

    async def _fetch_once(self) -> FetchResult:
        async with self._fetch_lock:
            result = await self._client.fetch_status()
            if result.snapshot is not None:
                self._snapshot = result.snapshot
            return result

    async def _refresh(
        self,
        *,
        initial: bool = False,
        broadcast: bool = True,
        record_fingerprint: bool = True,
    ) -> FetchResult:
        async with self._refresh_lock:
            result = await self._fetch_once()
            if result.snapshot is not None and not result.not_modified:
                await self._process_snapshot(
                    result.snapshot,
                    initial=initial,
                    broadcast=broadcast,
                    record_fingerprint=record_fingerprint,
                )
            return result

    async def _poll_loop(self) -> None:
        first = True
        # AstrBot 可能先加载插件、后完成 QQ/TG 适配器连接，给出站客户端
        # 一个短暂的启动窗口，避免首条推送过早失败。
        await asyncio.sleep(3)
        while True:
            delay = float(self.poll_interval)
            try:
                result = await self._refresh(initial=first)
                if result.snapshot is not None:
                    first = False
                elif result.not_modified:
                    first = False
                if result.retry_after is not None:
                    delay = max(delay, min(result.retry_after, 3600.0))
            except asyncio.CancelledError:
                raise
            except ResetApiError as exc:
                logger.warning("获取 Codex 重置状态失败：%s", exc)
                delay = min(max(delay, 120.0), 900.0)
            except Exception as exc:
                logger.exception("重置推送轮询异常：%s", exc)
                delay = min(max(delay, 120.0), 900.0)
            await asyncio.sleep(delay)

    async def _process_snapshot(
        self,
        snapshot: StatusSnapshot,
        *,
        initial: bool = False,
        broadcast: bool = True,
        record_fingerprint: bool = True,
    ) -> None:
        async with self._process_lock:
            parts = snapshot.fingerprint_parts()
            old = self._last_parts
            watch_changed = old is not None and parts.get("watch") != old.get("watch")
            reset_changed = old is not None and parts.get("reset") != old.get("reset")
            should_push_watch = (
                self.push_watch_updates
                and watch_changed
                and snapshot.active_watch_is_valid()
            )
            should_push_reset = (
                self.push_reset_announcements
                and reset_changed
                and snapshot.latest_reset is not None
            )

            self._snapshot = snapshot
            if record_fingerprint or old is None:
                self._last_parts = parts
            generated = snapshot.generated_at.isoformat() if snapshot.generated_at else None
            self._last_success_at = generated

            if broadcast and initial and self.push_on_start and self._targets:
                watch_valid = snapshot.active_watch_is_valid()
                if watch_valid or snapshot.latest_reset is not None:
                    if watch_valid and snapshot.latest_reset:
                        reason = "both"
                    elif watch_valid:
                        reason = "watch"
                    else:
                        reason = "reset"
                    await self._broadcast(
                        format_push(
                            snapshot,
                            reason=reason,
                            tz_name=self.timezone,
                            include_source=self.include_source,
                        )
                    )
            elif broadcast and (should_push_watch or should_push_reset):
                if should_push_watch and should_push_reset:
                    reason = "both"
                elif should_push_watch:
                    reason = "watch"
                else:
                    reason = "reset"
                if self._targets:
                    await self._broadcast(
                        format_push(
                            snapshot,
                            reason=reason,
                            tz_name=self.timezone,
                            include_source=self.include_source,
                        )
                    )
            await self._save_state()

    async def _broadcast(self, text: str) -> None:
        async with self._target_lock:
            targets = list(self._targets)
        if not targets:
            return
        for target in targets:
            try:
                # 不复用同一条 MessageChain，避免某些适配器发送时就地修改链。
                chain = MessageChain().message(text)
                result = await self.context.send_message(target, chain)
                if result is False:
                    logger.warning("主动发送失败（适配器返回 False）：%s", target)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("向目标 %s 推送重置消息失败：%s", target, exc)

    async def _get_snapshot_for_command(
        self, *, record_fingerprint: bool = False
    ) -> StatusSnapshot | None:
        try:
            # 查询命令只刷新缓存，不因一次手动查询触发全群推送。
            result = await self._refresh(
                initial=False,
                broadcast=False,
                record_fingerprint=record_fingerprint,
            )
            if (
                result.snapshot is not None
                and not result.not_modified
                and not record_fingerprint
            ):
                # 下一轮必须拿到 200 快照，才能比较并推送这次查询期间的
                # 变化，否则 ETag 304 会让后台轮询看不到未记录的指纹。
                self._client.invalidate_cache()
        except Exception as exc:
            logger.warning("/reset 刷新状态失败：%s", exc)
        return self._snapshot

    @filter.command("reset", alias={"重置时间", "codex_reset"}, priority=10)
    async def reset(self, event: AstrMessageEvent):
        """查询最新的社区重置预测。

        ``/codex_reset`` 是与内置 ``/reset`` 的备用别名。
        """

        snapshot = await self._get_snapshot_for_command()
        if snapshot is None:
            message = "【ChatGPT重置推送】暂时无法获取重置状态，请稍后再试。"
        else:
            message = format_status(
                snapshot,
                tz_name=self.timezone,
                include_source=self.include_source,
            )
        yield event.plain_result(message)
        # AstrBot 自带的 /reset 在部分版本中用于清空会话；要在结果产出后
        # 阻断后续处理，否则某些版本会连插件结果一起跳过。
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            stopper()

    @filter.command("reset_bind", alias={"订阅重置", "reset_subscribe"})
    async def reset_bind(self, event: AstrMessageEvent):
        """把当前 QQ 群/TG 群会话加入主动推送目标。"""

        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not umo:
            yield event.plain_result(
                "当前事件没有可用的 unified_msg_origin，无法订阅。"
            )
            return
        async with self._target_lock:
            if umo not in self._targets:
                self._targets.append(umo)
            count = len(self._targets)
            targets = list(self._targets)
            await self._save_targets_to_config(targets)
            await self._save_state(targets=targets)
        yield event.plain_result(
            f"已订阅当前会话的 ChatGPT 重置推送。当前目标数：{count}。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("reset_unbind", alias={"取消订阅重置", "reset_unsubscribe"})
    async def reset_unbind(self, event: AstrMessageEvent):
        """取消当前 QQ 群/TG 群会话的主动推送。"""

        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        async with self._target_lock:
            before = len(self._targets)
            self._targets = [target for target in self._targets if target != umo]
            removed = before != len(self._targets)
            count = len(self._targets)
            targets = list(self._targets)
            await self._save_targets_to_config(targets)
            await self._save_state(targets=targets)
        if removed:
            yield event.plain_result(f"已取消当前会话订阅。剩余目标数：{count}。")
        else:
            yield event.plain_result("当前会话不在推送目标中。")

    @filter.command("reset_whereami")
    async def reset_whereami(self, event: AstrMessageEvent):
        """显示当前 UMO，便于手工配置 TG 频道等无法接收命令的目标。"""

        umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if umo:
            yield event.plain_result(
                f"当前会话 UMO：{umo}\n可直接执行 /reset_bind 自动订阅。"
            )
        else:
            yield event.plain_result("当前事件没有 unified_msg_origin。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("reset_targets")
    async def reset_targets(self, event: AstrMessageEvent):
        """查看当前推送目标；UMO 本身不包含 bot token 等凭据。"""

        async with self._target_lock:
            targets = list(self._targets)
        if not targets:
            yield event.plain_result(
                "当前没有推送目标。请在目标群执行 /reset_bind。"
            )
            return
        yield event.plain_result(
            "当前推送目标：\n" + "\n".join(f"- {target}" for target in targets)
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("reset_push")
    async def reset_push(self, event: AstrMessageEvent):
        """立即把当前状态推送到所有已绑定目标，便于配置后测试。"""

        snapshot = await self._get_snapshot_for_command(record_fingerprint=True)
        if snapshot is None:
            yield event.plain_result("暂时无法获取重置状态，测试推送失败。")
            return
        async with self._target_lock:
            has_targets = bool(self._targets)
        if not has_targets:
            yield event.plain_result(
                "还没有推送目标，请先在目标群执行 /reset_bind。"
            )
            return
        await self._broadcast(
            format_push(
                snapshot,
                reason="both",
                tz_name=self.timezone,
                include_source=self.include_source,
            )
        )
        yield event.plain_result("已向所有推送目标发送测试推送。")

    async def terminate(self) -> None:
        task = self._poll_task
        self._poll_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._client.close()
