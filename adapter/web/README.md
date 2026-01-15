# Web 适配器说明

用于管理后台的 Web 对话功能（/webchat），通过 Redis 队列与机器人主程序交互。

关键点：
- 需要启用 `adapter.enabled=true` 与 `web.enable=true`
- 需要可用的 Redis（用于写入主队列与读取回复队列）
- 默认队列：主队列 `allbot`，回复队列 `allbot_reply:web`
