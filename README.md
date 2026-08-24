# ChatGPT/Codex 重置推送 AstrBot 插件

这个插件会把新的社区预测/公告推送到指定的 QQ 群、Telegram 群或 Telegram 频道，并提供
`/reset` 等查询命令。

## 前提

需要astrbot

## 安装

1.WebUI中上传压缩包安装

2.通过链接安装 

```
https://github.com/ecxwxz/codexreset-astrbot-plugin/releases/download/v1.0/astrbot_plugin_codex_reset.zip
```

## 最快配置：在目标群执行绑定命令

1. 确认 AstrBot 已连接 OneBot/QQ（`aiocqhttp`）或 Telegram。
2. 在想接收推送的 QQ 群或 Telegram 群里发送：

   ```text
   /reset_bind
   ```

3. 如需测试（管理员），发送 `/reset_push`；如需查询，发送 `/reset`。

绑定会保存当前会话的 UMO（Unified Message Origin），重启后仍然保留。管理员可用
`/reset_unbind` 取消绑定，用 `/reset_targets` 查看目标。

## Telegram 频道与手工 UMO

某些 Telegram 适配器版本不会把频道的 `channel_post` 作为可触发命令的事件，因此
频道通常无法执行 `/reset_bind` 或 `/reset_whereami`。群聊可用
`/reset_whereami` 查看 UMO；频道请从 Bot API/适配器日志取得 chat ID，再手工填写
`telegram:GroupMessage:<频道 chat_id>`（通常已经是 `-100...`，且需要机器人拥有发言权限）。
常见形式如下（实际平台实例名以命令输出为准）：

```text
aiocqhttp:GroupMessage:123456789
telegram:GroupMessage:-1001234567890
telegram:GroupMessage:-1001234567890#456
```

Telegram 频道/群需要给机器人发送消息权限；QQ 官方机器人适配器不一定支持通用
主动发送，推荐使用 OneBot `aiocqhttp`。如果配置了多个 Telegram/QQ 适配器，UMO
里的第一段必须使用实际的适配器实例 ID。

## 指令

| 指令 | 作用 |
| --- | --- |
| `/reset` | 获取最新预测和最近一次公告（北京时间） |
| `/codex_reset` | `/reset` 的备用别名；可避开 AstrBot 内置 `/reset` 冲突 |
| `/reset_bind` | 将当前会话加入推送目标 |
| `/reset_unbind` | 移除当前会话（管理员） |
| `/reset_whereami` | 显示当前 UMO，方便手工配置频道 |
| `/reset_targets` | 查看目标列表（管理员） |
| `/reset_push` | 立即向所有目标发送一次测试推送（管理员） |

插件会在启动后先记录当前指纹，不追发历史消息（除非开启 `push_on_start`）。之后
只在预测窗口或重置公告发生变化时推送，并使用 ETag、处理 304/429/503，避免频繁
请求公共 API。

> 插件接管了 `/reset`，用于返回重置状态；这会覆盖 AstrBot 部分版本中“清空会话”的
> 同名内置指令。`/codex_reset` 是一个不易混淆的查询别名；若仍需要清空会话，请暂时
> 停用本插件或使用 AstrBot 的其他会话管理方式。

## 配置项摘要

- `api_url`：默认 `https://codex-resets.com/api/v1/status`；替换时需返回同样的
  v1 JSON 字段（`data.latest_reset`、`data.active_watch`）。接口结构可参考
  [OpenAPI 文档](https://codex-resets.com/api/openapi.json)。
- `poll_interval_seconds`：默认 120 秒，代码会限制为至少 60 秒。
- `targets`：UMO 列表；命令绑定会自动同步。
- `timezone`：默认 `Asia/Shanghai`。
- `push_watch_updates` / `push_reset_announcements`：分别控制预测和新公告推送。
- `send_source`：是否在消息中附带 X/社区来源链接。

## 说明

网站的“预计时间”是预测窗口的截止/边界时间，所以插件文案会使用“预计”“前后”
和“社区预测”等措辞，不会把它伪装成精确的 OpenAI 官方时间。服务不可用时，插件
会保留最近一次成功结果，后台轮询不会因此退出。
