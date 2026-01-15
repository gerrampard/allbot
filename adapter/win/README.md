# Win 适配器说明

用于对接 Windows 侧的消息通道（通常是本地/局域网的 WebSocket/HTTP 服务），将收到的消息写入 Redis 队列并读取回复。

关键点：
- 需要配置 `win.wsUrl` / `win.sendUrl` 等通信参数
- 需要可用的 Redis
- 默认队列：主队列 `allbot`，回复队列 `allbot_reply:win`
