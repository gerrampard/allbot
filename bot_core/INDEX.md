<!-- AUTO-DOC: Update me when files in this folder change -->

# bot_core

启动编排核心：负责客户端初始化、登录处理、服务装配与消息监听。已增加 869 协议分支，并将微信登录改为后台异步任务，避免阻塞适配器消息链路。

## Files

| File | Role | Function |
|------|------|----------|
| __init__.py | Entry | 导出 `bot_core` 与消息监听函数 |
| orchestrator.py | Orchestrator | 串联启动流程（登录异步化，不阻塞适配器） |
| client_initializer.py | Client Builder | 按协议创建客户端（含 `Client869`）并接入回复路由 |
| login_handler.py | Login | 登录/会话恢复（含 869 二维码轮询、必要时自动切换 mac 滑块流程；状态回调补齐 device_name/device_id） |
| service_initializer.py | Service Init | 数据库、插件、通知等初始化 |
| message_listener.py | Message IO | WS 收消息标准化、入队与消费调度（869 在扫码登录成功后再连主 WS；掉线时触发免扫码唤醒登录；key 优先 token_key/auth_key） |
| ws_message_normalizer.py | Utility | WS 消息数组提取与 AddMsgs 归一化（兼容 `{str:...}`/`{string:...}` 字段结构） |
| status_manager.py | Status | 全局运行状态管理 |
